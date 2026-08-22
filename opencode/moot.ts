// moot — OpenCode plugin: joins this OpenCode process to a running moot floor.
// Install: symlink this file into ~/.config/opencode/plugins/ (or a project's
// .opencode/plugins/) and start OpenCode with MOOT_NAME=<name> [MOOT_ROLE=…].
// Everything except the plugin itself lives in ./lib.ts — OpenCode calls every
// runtime export of this file as a plugin factory, so there must be one.

import { tool, type Plugin } from "@opencode-ai/plugin"
import process from "node:process"

import {
  Connection,
  HELD_CAP,
  render,
  resolveHome,
  SAY_TIMEOUT_MS,
  type Frame,
  type Logger,
} from "./lib.ts"

// ------------------------------------------------------------------- plugin

const TOOL_DESCRIPTION =
  "Say something on the moot floor to the other agents. " +
  "Address one peer with to:'beta', everyone with to:'*'; " +
  "kinds: answer, claim, done, note, objection, question, result; " +
  "private:true for a message no other agent may read"

export const MootPlugin: Plugin = async ({ client, $ }) => {
  const name = process.env["MOOT_NAME"]
  const role = process.env["MOOT_ROLE"] ?? ""
  const log: Logger = (level, message) => {
    void client.app
      .log({ body: { service: "moot", level, message } })
      .catch((error: unknown) => console.error(`moot: log failed: ${String(error)}`))
  }

  if (name === undefined || name === "") {
    log("warn", "MOOT_NAME is not set — the moot plugin stays inert")
    return {}
  }

  const home = resolveHome()
  let activeSessionID: string | null = null
  /** The state the hub should be holding for this spoke, so a reconnect can
   *  restate it: a reclaim resets the registration to `idle` (PROTOCOL
   *  "hello") even in the middle of a busy turn. */
  let reportedState: "idle" | "busy" | null = null
  /** Sessions that already carry the operating rules. The seat can move to a
   *  session that has never seen them, and that model needs them too. */
  const briefed = new Set<string>()
  let hubClosedNotified = false
  /** Sessions that are a subagent task, by id: never the floor's seat.
   *  `session.idle` carries only a sessionID, so the parentage has to be
   *  remembered from `session.created`. */
  const children = new Set<string>()
  const held: string[] = []
  let heldDropped = 0
  /** Injections run one at a time, in arrival order. */
  let chain: Promise<void> = Promise.resolve()
  /** The join line, kept so the first prompt of every session says who is on
   *  the floor — the log line the welcome writes is the human's, not the
   *  model's. */
  let welcomeLine: string | null = null

  const briefing = async (): Promise<string> => {
    try {
      return (
        await $`moot brief --runtime opencode --name ${name} --role ${role}`.text()
      ).trim()
    } catch (error) {
      // Boundary: `moot` may not be on OpenCode's PATH. The session still has
      // to know what it is looking at, so say so in two lines and log why.
      const reason = error instanceof Error ? error.message : String(error)
      log("error", `\`moot brief\` failed (${reason}) — using the built-in notice`)
      return [
        `[moot] The operating rules could not be loaded: \`moot brief\` failed (${reason}); put \`moot\` on OpenCode's PATH.`,
        `[moot] You are \`${name}\` on a shared floor: answer only when a message is addressed to you, answer once, and send with the moot_say tool.`,
      ].join("\n")
    }
  }

  const inject = async (text: string): Promise<void> => {
    const id = activeSessionID
    if (id === null) {
      held.push(text) // flushed by the first session.idle
      // Drop-oldest at the hub's own wake-queue cap: a process that never
      // opens a session must not grow this without bound.
      while (held.length > HELD_CAP) {
        held.shift()
        heldDropped += 1
      }
      return
    }
    let body = text
    if (!briefed.has(id)) {
      briefed.add(id) // before the await: the next delivery must not prepend it again
      const head = welcomeLine === null ? "" : `${welcomeLine}\n`
      body = `${head}${await briefing()}\n\n${text}`
    }
    await client.session.prompt({
      path: { id },
      body: { parts: [{ type: "text", text: body }] },
    })
  }

  const injectSoon = (text: string): void => {
    // session.prompt() returns only when the model's reply is complete, so a
    // delivery is fire-and-forget: awaiting it would park the hub reader for
    // a whole turn (P0.2). One chain, not N parallel prompts — arrival order
    // is the reading order, and the briefing really does go first.
    chain = chain
      .then(() => inject(text))
      .catch((error: unknown) => log("error", `delivery failed: ${String(error)}`))
  }

  const conn = new Connection({
    home,
    name,
    kind: "opencode",
    role,
    log,
    onWelcome: (frame) => {
      if (hubClosedNotified) {
        hubClosedNotified = false
        injectSoon("[moot] rejoined the floor")
      }
      // A fresh registration and a reclaim both start at `idle` on the hub;
      // if this process is mid-turn, say so now instead of letting the hub
      // deliver into a busy session until the next OpenCode event.
      if (reportedState !== null) conn.send({ t: "state", state: reportedState })
      frame["kind"] = "opencode" // the hub's welcome omits the joiner's own kind
      const lines = render(frame)
      welcomeLine = lines[0] ?? null
      for (const line of lines) log("info", line)
    },
    onFrame: (frame) => {
      const lines = render(frame)
      // One prompt per frame, not per line: a coalesced `deliver` is one turn.
      if (lines.length > 0) injectSoon(lines.join("\n"))
    },
    onHubClosed: () => {
      if (hubClosedNotified) return
      hubClosedNotified = true
      injectSoon("[moot] hub closed — reconnecting; do not send until it is back")
    },
  })
  conn.start()

  const adopt = (sessionID: string): boolean => {
    if (activeSessionID === null) activeSessionID = sessionID
    return sessionID === activeSessionID
  }

  return {
    event: async ({ event }) => {
      if (event.type === "session.created") {
        // A child session (a subagent task) is never the floor's seat.
        if (event.properties.info.parentID) {
          children.add(event.properties.info.id)
          return
        }
        adopt(event.properties.info.id)
        return
      }
      if (event.type === "session.idle") {
        const id = event.properties.sessionID
        // One user, one seat: whichever top-level session last finished a
        // turn is the one that is being read, so it takes the seat (P5.4).
        if (!children.has(id)) activeSessionID = id
        if (id !== activeSessionID) return
        if (reportedState !== "idle") {
          reportedState = "idle"
          conn.send({ t: "state", state: "idle" })
        }
        const waiting = held.splice(0, held.length)
        if (heldDropped > 0 && waiting.length > 0) {
          log("warn", `${heldDropped} held message(s) dropped before a session existed`)
          waiting[0] = `[moot] ${heldDropped} earlier message(s) were dropped\n${waiting[0]}`
          heldDropped = 0
        }
        for (const text of waiting) injectSoon(text)
        return
      }
      if (event.type === "session.status") {
        const id = event.properties.sessionID
        if (children.has(id) || !adopt(id)) return
        // The event repeats through a turn; the hub wants the transition.
        if (event.properties.status.type === "busy" && reportedState !== "busy") {
          reportedState = "busy"
          conn.send({ t: "state", state: "busy" })
        }
      }
    },

    tool: {
      moot_say: tool({
        description: TOOL_DESCRIPTION,
        args: {
          to: tool.schema
            .string()
            .default("*")
            .describe("a peer's name, or '*' for everyone"),
          kind: tool.schema
            .string()
            .default("note")
            .describe("answer, claim, done, note, objection, question or result"),
          text: tool.schema.string().describe("what to say, verbatim"),
          private: tool.schema
            .boolean()
            .default(false)
            .describe(
              "no other agent sees it; needs a named peer — a reply is public unless marked private too",
            ),
        },
        async execute(args) {
          const reply = await conn.say(args.to, args.kind, args.text, args.private)
          if (reply["t"] === "ok") {
            const parts = [`ok → ${args.to}`]
            const id = reply["id"]
            if (typeof id === "number" && id > 0) parts.push(`#${id}`)
            if (args.private) parts.push("private")
            const queued = reply["queued"]
            if (typeof queued === "number" && queued > 0) {
              parts.push(`queued at ${queued} busy peer(s)`)
            }
            return parts.join(" · ")
          }
          const line = `err ${String(reply["code"])}: ${String(reply["detail"])}`
          const retryAfter = reply["retry_after"]
          if (retryAfter === undefined || retryAfter === null) return line
          return `${line} · retry in ${String(retryAfter)}s`
        },
      }),
    },

    dispose: async () => {
      conn.stop()
    },
  }
}
