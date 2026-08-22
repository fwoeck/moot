/**
 * `bun test opencode/` — the two halves of the plugin that can be tested
 * without OpenCode: the renderer (against the fixture the Python spoke uses,
 * so the two can't drift) and the Connection against a real `moot serve`.
 *
 * The hub runs as a child process in a short /tmp home (macOS caps AF_UNIX
 * paths at 104 bytes) with its own $HOME, so the rendezvous file it writes
 * never touches the developer's ~/.moot/current.
 */

import { afterAll, beforeAll, describe, expect, test } from "bun:test"
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs"
import { createConnection, type Socket } from "node:net"
import { join } from "node:path"
import process from "node:process"

import { Connection, render, type Frame } from "./lib.ts"

const REPO = join(import.meta.dir, "..")

// ---------------------------------------------------------------- rendering

interface RenderCase {
  name: string
  frame: Frame
  lines: string[]
}

const CASES: RenderCase[] = JSON.parse(
  readFileSync(join(REPO, "tests", "fixtures", "render_cases.json"), "utf8"),
) as RenderCase[]

describe("render", () => {
  test("the fixture covers every frame type", () => {
    const types = new Set(CASES.map((c) => c.frame["t"]))
    for (const t of ["welcome", "deliver", "event", "err", "ok", "roster"]) {
      expect(types).toContain(t)
    }
  })

  for (const c of CASES) {
    test(c.name, () => {
      expect(render(c.frame)).toEqual(c.lines)
    })
  }

  test("an unknown frame type renders nothing", () => {
    expect(render({ t: "banana" })).toEqual([])
  })

  test("a malformed deliver raises", () => {
    expect(() => render({ t: "deliver", round: 1 })).toThrow("msgs list")
  })
})

// -------------------------------------------------------------- a real hub

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function waitFor(what: string, cond: () => boolean, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs
  while (!cond()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`)
    await sleep(20)
  }
}

/** A second participant, spoken to the hub by hand — the plugin's Connection
 *  is the code under test, so its peer must not share its implementation. */
class RawPeer {
  readonly frames: Frame[] = []
  private buffer = ""

  private constructor(private readonly socket: Socket) {}

  static async join(home: string, name: string): Promise<RawPeer> {
    const socket = createConnection({ path: join(home, "hub.sock") })
    socket.setEncoding("utf8")
    const peer = new RawPeer(socket)
    socket.on("data", (chunk: string | Buffer) => peer.onData(chunk))
    await new Promise<void>((resolve, reject) => {
      socket.once("connect", () => resolve())
      socket.once("error", reject)
    })
    peer.send({ t: "hello", proto: 1, name, kind: "opencode", role: "", caps: [] })
    await waitFor(`${name}'s welcome`, () => peer.frames.some((f) => f["t"] === "welcome"))
    return peer
  }

  private onData(chunk: string | Buffer): void {
    this.buffer += typeof chunk === "string" ? chunk : chunk.toString("utf8")
    for (;;) {
      const cut = this.buffer.indexOf("\n")
      if (cut < 0) return
      const line = this.buffer.slice(0, cut)
      this.buffer = this.buffer.slice(cut + 1)
      if (line.trim() !== "") this.frames.push(JSON.parse(line) as Frame)
    }
  }

  send(frame: Frame): void {
    this.socket.write(`${JSON.stringify(frame)}\n`)
  }

  close(): void {
    this.socket.destroy()
  }
}

describe("Connection against a real hub", () => {
  let home = ""
  let fakeHome = ""
  let hub: Bun.Subprocess | null = null

  // Only the spawn happens here: bun caps a hook at 5 s, and a cold `uv run`
  // can outlast that. Waiting for the socket belongs to the test's own budget.
  beforeAll(() => {
    home = mkdtempSync("/tmp/moot-bun-")
    fakeHome = mkdtempSync("/tmp/moot-bunhome-")
    hub = Bun.spawn(["uv", "run", "moot", "serve", "--home", home, "--no-notify"], {
      cwd: REPO,
      env: { ...process.env, HOME: fakeHome },
      stdout: "ignore",
      stderr: "ignore",
    })
  })

  afterAll(async () => {
    if (hub !== null) {
      hub.kill("SIGTERM") // by pid, this child only
      await hub.exited
    }
    for (const dir of [home, fakeHome]) {
      if (dir !== "") rmSync(dir, { recursive: true, force: true })
    }
  })

  test("hello/welcome, a delivery from a peer, and a say that is acked", async () => {
    await waitFor("the hub socket", () => existsSync(join(home, "hub.sock")), 20_000)
    const welcomes: Frame[] = []
    const inbound: Frame[] = []
    const closed: string[] = []
    const conn = new Connection({
      home,
      name: "beta",
      kind: "opencode",
      role: "tests",
      log: () => {},
      onWelcome: (frame) => welcomes.push(frame),
      onFrame: (frame) => inbound.push(frame),
      onHubClosed: () => closed.push("closed"),
    })
    conn.start()
    try {
      await waitFor("beta's welcome", () => welcomes.length > 0)
      expect(welcomes[0]!["t"]).toBe("welcome")
      expect(welcomes[0]!["name"]).toBe("beta")
      expect(conn.connected).toBe(true)

      // caps: ["idle-events"] means the hub queues until the spoke says idle.
      conn.send({ t: "state", state: "idle" })

      const gamma = await RawPeer.join(home, "gamma")
      try {
        gamma.send({
          t: "say",
          to: "beta",
          kind: "question",
          text: "which migration ran last?",
          seq: 1,
        })
        await waitFor("the deliver", () => inbound.some((f) => f["t"] === "deliver"))
        const deliver = inbound.find((f) => f["t"] === "deliver")!
        expect(render(deliver)).toEqual([
          "[r1 #1] gamma → beta · question: which migration ran last?",
        ])

        const reply = await conn.say("gamma", "answer", "the index is missing")
        expect(reply["t"]).toBe("ok")
        await waitFor(
          "gamma's deliver",
          () => gamma.frames.some((f) => f["t"] === "deliver"),
        )
      } finally {
        gamma.close()
      }
      expect(closed).toEqual([])
    } finally {
      conn.stop()
    }
  }, 25_000)

  test("a private say carries the flag on the wire; a third peer never sees it", async () => {
    await waitFor("the hub socket", () => existsSync(join(home, "hub.sock")), 20_000)
    const welcomes: Frame[] = []
    const conn = new Connection({
      home,
      name: "delta",
      kind: "opencode",
      role: "tests",
      log: () => {},
      onWelcome: (frame) => welcomes.push(frame),
      onFrame: () => {},
      onHubClosed: () => {},
    })
    conn.start()
    try {
      await waitFor("delta's welcome", () => welcomes.length > 0)
      const zeta = await RawPeer.join(home, "zeta")
      const eta = await RawPeer.join(home, "eta")
      try {
        const reply = await conn.say("zeta", "claim", "psst", true)
        expect(reply["t"]).toBe("ok")
        await waitFor("zeta's deliver", () => zeta.frames.some((f) => f["t"] === "deliver"))
        // the first wake also carries the late-join backlog as context; the
        // private line is the one direct message and the only flagged one
        const deliver = zeta.frames.find((f) => f["t"] === "deliver")!
        const msgs = deliver["msgs"] as Record<string, unknown>[]
        const direct = msgs.filter((m) => m["addressing"] === "direct")
        expect(direct.map((m) => [m["text"], m["private"]])).toEqual([["psst", true]])
        expect(msgs.filter((m) => m["private"] === true)).toHaveLength(1)
        const lines = render(deliver)
        expect(lines.at(-1)).toMatch(/^\[r\d+ #\d+\] delta → zeta · private · claim: psst$/)

        // eta's next wake is the proof: its backlog holds the public history,
        // never the private line
        const pub = await conn.say("eta", "note", "wach")
        expect(pub["t"]).toBe("ok")
        await waitFor("eta's deliver", () => eta.frames.some((f) => f["t"] === "deliver"))
        const etaDeliver = eta.frames.find((f) => f["t"] === "deliver")!
        const etaMsgs = etaDeliver["msgs"] as Record<string, unknown>[]
        const etaTexts = etaMsgs.map((m) => m["text"])
        expect(etaTexts).toContain("wach")
        expect(etaTexts).not.toContain("psst")
        expect(etaMsgs.every((m) => m["private"] === undefined)).toBe(true)
      } finally {
        zeta.close()
        eta.close()
      }
    } finally {
      conn.stop()
    }
  }, 25_000)

  // A rejected hello used to reach onFrame (the err carries `seq: null`), so
  // the plugin injected it as a prompt and the 5 s reconnect loop re-injected
  // it forever — one model turn every 5 s for as long as OpenCode ran.
  test("a rejected hello is logged, never handed to onFrame, and gives up", async () => {
    await waitFor("the hub socket", () => existsSync(join(home, "hub.sock")), 20_000)
    const squatter = await RawPeer.join(home, "epsilon")

    const taken = { errors: [] as string[], frames: [] as Frame[], welcomes: 0 }
    const malformed = { errors: [] as string[], frames: [] as Frame[], welcomes: 0 }
    const watch = (into: typeof taken) => ({
      log: (level: string, message: string) => {
        if (level === "error") into.errors.push(message)
      },
      onWelcome: () => {
        into.welcomes += 1
      },
      onFrame: (frame: Frame) => into.frames.push(frame),
      onHubClosed: () => {},
    })

    const conn = new Connection({ home, name: "epsilon", kind: "opencode", role: "", ...watch(taken) })
    // role > 256 chars (PROTOCOL "Participants") — `malformed`. The hub keeps
    // that connection open, so nothing but the plugin itself stops it.
    const bad = new Connection({
      home,
      name: "zeta",
      kind: "opencode",
      role: "x".repeat(300),
      ...watch(malformed),
    })
    conn.start()
    bad.start()
    try {
      await waitFor("the name_taken rejection", () => taken.errors.length > 0)
      await waitFor("the malformed rejection", () => malformed.errors.length > 0)
      expect(taken.errors[0]).toContain("name_taken")
      expect(taken.errors[0]).toContain("epsilon-2") // the free-name suggestion
      expect(malformed.errors[0]).toContain("malformed")

      await sleep(6_500) // one full reconnect interval and then some
      expect(taken.errors).toHaveLength(1) // gave up instead of looping
      expect(taken.frames).toEqual([]) // and never asked for an injection
      expect(taken.welcomes).toBe(0)
      expect(conn.connected).toBe(false)
      expect(malformed.errors).toHaveLength(1)
      expect(malformed.frames).toEqual([])
      expect(bad.connected).toBe(false)
    } finally {
      conn.stop()
      bad.stop()
      squatter.close()
    }
  }, 25_000)

  test("a say without a hub is answered locally, not left hanging", async () => {
    const conn = new Connection({
      home: join(home, "nowhere"),
      name: "delta",
      kind: "opencode",
      role: "",
      log: () => {},
      onWelcome: () => {},
      onFrame: () => {},
      onHubClosed: () => {},
    })
    conn.start()
    try {
      const reply = await conn.say("gamma", "note", "anyone there?")
      expect(reply["code"]).toBe("hub_unreachable")
      expect(conn.connected).toBe(false)
    } finally {
      conn.stop()
    }
  })
})
