// moot plugin internals — shared by the plugin entry (moot.ts) and the bun
// test. Split out because OpenCode's loader calls EVERY runtime export of a
// plugin file as a plugin factory ("Plugin export is not a function" on a
// non-function export), so moot.ts must export exactly one thing.
/**
 * The moot spoke for OpenCode: one plugin file, no build step.
 *
 * It holds this OpenCode process's connection to the hub (NDJSON over the
 * unix socket at `<home>/hub.sock`, docs/PROTOCOL.md), turns inbound frames
 * into prompts on the active session, reports real turn boundaries from
 * OpenCode's own events, and gives the model a `moot_say` tool.
 *
 * `render()` is a port of src/moot/spoke/render.py — the two are pinned to
 * the same fixture (tests/fixtures/render_cases.json), so a change here is a
 * change there. `render` and `Connection` carry no module-level side effects
 * and are importable without OpenCode (opencode/moot.test.ts does that).
 *
 * Install: symlink this file into ~/.config/opencode/plugins/moot.ts and
 * start OpenCode with `MOOT_NAME=beta [MOOT_ROLE="…"] opencode`.
 *
 * One process holds one name and one seat: deliveries go to the top-level
 * session that most recently finished a turn (a subagent task session never
 * takes the seat), which assumes the single user the moot is built for. If
 * the hub rejects the `hello` — the name is taken by a live connection, or
 * the protocol versions differ — the plugin logs one line and stays inert
 * for the rest of the process; restart OpenCode with a free MOOT_NAME.
 */

import { existsSync, readFileSync } from "node:fs"
import { createConnection, type Socket } from "node:net"
import { homedir } from "node:os"
import { join } from "node:path"
import process from "node:process"

export type Frame = { [key: string]: unknown }

export type LogLevel = "debug" | "info" | "warn" | "error"
export type Logger = (level: LogLevel, message: string) => void

const PROTO_VERSION = 1
const RECONNECT_MS = 5_000
/** Deliveries held until a session exists, capped like the hub's wake queue. */
export const HELD_CAP = 100
export const SAY_TIMEOUT_MS = 10_000

// ---------------------------------------------------------------- rendering

function asRecord(value: unknown, what: string): Frame {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${what} is not an object: ${JSON.stringify(value)}`)
  }
  return value as Frame
}

function items(frame: Frame, key: string): Frame[] {
  const value = frame[key]
  if (!Array.isArray(value)) {
    throw new Error(`'${String(frame["t"])}' frame without a ${key} list`)
  }
  return value.map((item) => asRecord(item, `${key} entry`))
}

function deliverLines(frame: Frame): string[] {
  return items(frame, "msgs").map((m) => {
    const id = m["id"]
    // No id, no `#tag` — the hub's placeholders carry 0.
    const tag = typeof id === "number" && id > 0 ? ` #${String(id)}` : ""
    const label =
      m["addressing"] === "overheard" ? "context" : `r${String(frame["round"])}`
    const mark = m["private"] === true ? " · private" : ""
    return `[${label}${tag}] ${String(m["from"])} → ${String(m["to"])}${mark} · ${String(m["kind"])}: ${String(m["text"])}`
  })
}

function peerList(frame: Frame): string {
  const peers = items(frame, "peers").map((p) => {
    const role = p["role"]
    // A role belongs to an agent; the observer's is not floor information.
    const shown =
      p["kind"] !== "observer" && typeof role === "string" && role !== ""
        ? `${String(p["kind"])}, ${role}`
        : String(p["kind"])
    return `${String(p["name"])} (${shown}, ${String(p["state"])})`
  })
  return peers.length > 0 ? peers.join(", ") : "none"
}

function welcomeLine(frame: Frame): string {
  // The hub's welcome carries no kind for the joiner itself (PROTOCOL.md
  // "welcome"); the plugin knows it and adds it before rendering.
  const kind = frame["kind"]
  const who =
    typeof kind === "string"
      ? `${String(frame["name"])} (${kind})`
      : `${String(frame["name"])}`
  const limits = frame["limits"]
  const maxRounds =
    typeof limits === "object" && limits !== null && !Array.isArray(limits)
      ? (limits as Frame)["max_rounds"]
      : undefined
  const rounds =
    maxRounds === undefined || maxRounds === null
      ? `${String(frame["round"])}`
      : `${String(frame["round"])}/${String(maxRounds)}`
  return `[moot] joined as ${who} · peers: ${peerList(frame)} · round ${rounds}`
}

/** Inbound frames as text for a model. Identical to spoke/render.py. */
export function render(frame: Frame): string[] {
  const t = frame["t"]
  if (t === "deliver") return deliverLines(frame)
  if (t === "event") {
    const rest = Object.entries(frame)
      .filter(([key]) => key !== "t" && key !== "event")
      .map(([key, value]) => `${key}=${String(value)}`)
      .join(" ")
    const head = `[event] ${String(frame["event"])}`
    return [rest !== "" ? `${head} · ${rest}` : head]
  }
  if (t === "err") {
    const line = `[err] ${String(frame["code"])} · ${String(frame["detail"])}`
    const retryAfter = frame["retry_after"]
    // the hub's rate-limit detail is a constant; the wait is only here
    if (retryAfter === undefined || retryAfter === null) return [line]
    return [`${line} · retry in ${String(retryAfter)}s`]
  }
  if (t === "ok") return [] // confirmations are noise for the reader; errs are not
  if (t === "roster") {
    return items(frame, "peers").map(
      (p) => `[roster] ${String(p["name"])} (${String(p["kind"])}, ${String(p["state"])})`,
    )
  }
  if (t === "welcome") return [welcomeLine(frame)]
  return []
}

// --------------------------------------------------------------------- home

function expandUser(path: string): string {
  if (path === "~") return homedir()
  if (path.startsWith("~/")) return join(homedir(), path.slice(2))
  return path
}

/** `$MOOT_HOME` → the rendezvous file `~/.moot/current` → `~/.moot`.
 *  Mirrors spoke/home.py resolve_home() for a spoke with no `--home`. */
export function resolveHome(): string {
  const env = process.env["MOOT_HOME"]
  if (env !== undefined && env !== "") return expandUser(env)
  const current = join(homedir(), ".moot", "current")
  if (existsSync(current)) {
    const recorded = readFileSync(current, "utf8").trim()
    // A stale file (hub gone, socket removed) must not shadow ~/.moot.
    if (recorded !== "" && existsSync(join(recorded, "hub.sock"))) return recorded
  }
  return join(homedir(), ".moot")
}

// --------------------------------------------------------------- connection

export interface ConnectionOptions {
  home: string
  name: string
  kind: string
  role: string
  log: Logger
  /** The hub's answer to `hello`. Not a delivery — never injected. */
  onWelcome: (frame: Frame) => void
  /** Every other unsolicited frame: deliver, event, asynchronous err, roster. */
  onFrame: (frame: Frame) => void
  /** A connection that was live is gone; a reconnect is already scheduled. */
  onHubClosed: () => void
}

/**
 * One name, one connection, for the lifetime of the OpenCode process.
 *
 * Nothing here throws into Bun's event loop: a transport failure schedules a
 * reconnect, and a `say` waiting for its `ok` is answered with an `err` frame
 * rather than left hanging.
 */
export class Connection {
  private socket: Socket | null = null
  private live = false
  private welcomed = false
  private buffer = ""
  private seq = 0
  private readonly pending = new Map<number, (frame: Frame) => void>()
  private retry: ReturnType<typeof setTimeout> | null = null
  private outageLogged = false
  private stopped = false

  constructor(private readonly options: ConnectionOptions) {}

  get connected(): boolean {
    return this.live
  }

  get socketPath(): string {
    return join(this.options.home, "hub.sock")
  }

  start(): void {
    this.open()
  }

  stop(): void {
    this.stopped = true
    if (this.retry !== null) {
      clearTimeout(this.retry)
      this.retry = null
    }
    const socket = this.socket
    this.socket = null
    this.live = false
    this.welcomed = false
    if (socket !== null) socket.destroy()
    this.failPending("the plugin is shutting down")
  }

  /** True if the frame reached the socket. A frame sent before the hub's
   *  `welcome` — or while the hub is down — is dropped rather than answered
   *  with `err: malformed` ("hello expected first"): the state the hub needs
   *  is resent from onWelcome, and `say` gets its `err` from say() below. */
  send(frame: Frame): boolean {
    const socket = this.socket
    if (socket === null || !this.welcomed) return false
    socket.write(`${JSON.stringify(frame)}\n`)
    return true
  }

  /** Resolves with the hub's `ok`/`err` for this `say`, or a locally made
   *  `err` frame (`timeout`, `hub_unreachable`). */
  async say(
    to: string,
    kind: string,
    text: string,
    priv: boolean = false,
    timeoutMs: number = SAY_TIMEOUT_MS,
  ): Promise<Frame> {
    this.seq += 1
    const seq = this.seq
    const frame: Frame = { t: "say", to, kind, text, seq }
    if (priv) frame["private"] = true // the key rides only when set (PROTOCOL)
    if (!this.send(frame)) {
      return { t: "err", code: "hub_unreachable", detail: "no connection to the hub" }
    }
    return await new Promise<Frame>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(seq)
        resolve({ t: "err", code: "timeout", detail: "no reply from hub" })
      }, timeoutMs)
      this.pending.set(seq, (frame) => {
        clearTimeout(timer)
        resolve(frame)
      })
    })
  }

  private open(): void {
    if (this.stopped) return
    const socket = createConnection({ path: this.socketPath })
    this.socket = socket
    socket.setEncoding("utf8")
    socket.on("connect", () => {
      this.live = true
      this.welcomed = false
      this.outageLogged = false
      this.buffer = ""
      this.options.log("info", `connected to ${this.socketPath}`)
      // The one frame that goes out before `welcome`, so it bypasses send().
      const hello: Frame = {
        t: "hello",
        proto: PROTO_VERSION,
        name: this.options.name,
        kind: this.options.kind,
        role: this.options.role,
        caps: ["idle-events"],
      }
      socket.write(`${JSON.stringify(hello)}\n`)
    })
    socket.on("data", (chunk: string | Buffer) => this.onData(chunk))
    socket.on("error", (error: Error) => this.down(socket, error.message))
    socket.on("close", () => this.down(socket, "the hub closed the connection"))
  }

  private onData(chunk: string | Buffer): void {
    this.buffer += typeof chunk === "string" ? chunk : chunk.toString("utf8")
    for (;;) {
      if (this.stopped) return // dispatch gave up on this connection
      const cut = this.buffer.indexOf("\n")
      if (cut < 0) return
      const line = this.buffer.slice(0, cut)
      this.buffer = this.buffer.slice(cut + 1)
      if (line.trim() === "") continue // empty lines are not frames
      try {
        this.dispatch(line)
      } catch (error) {
        // A frame the plugin cannot read means the two sides disagree about
        // the wire: drop the connection loudly and re-handshake rather than
        // let the exception escape into Bun's loop and take OpenCode down.
        this.options.log("error", `unreadable hub frame (${String(error)}) — reconnecting`)
        this.down(this.socket, "the plugin could not read a frame")
        return
      }
    }
  }

  private dispatch(line: string): void {
    const frame = asRecord(JSON.parse(line), "hub frame")
    if (frame["t"] === "ping") {
      this.send({ t: "pong" })
      return
    }
    if (frame["t"] === "welcome") {
      this.welcomed = true
      this.options.onWelcome(frame)
      return
    }
    if (!this.welcomed && frame["t"] === "err") {
      // The hello was rejected (PROTOCOL "hello": name_taken, proto_mismatch,
      // malformed) — hello is the only frame send() lets out before the
      // welcome, so a pre-welcome err is always about it. It is not a
      // delivery: injecting it would cost one model turn per reconnect,
      // forever, and the next hello would be identical anyway. Log it — the
      // hub's detail carries the free-name suggestion — and give up, like
      // spoke/conn.py, which raises HubError instead of retrying.
      this.options.log(
        "error",
        `hello rejected: ${String(frame["code"])} · ${String(frame["detail"])} — the moot plugin is inert for this process`,
      )
      this.stop()
      return
    }
    const seq = frame["seq"]
    if (typeof seq === "number") {
      const resolve = this.pending.get(seq)
      if (resolve !== undefined) {
        this.pending.delete(seq)
        resolve(frame)
        return
      }
    }
    this.options.onFrame(frame)
  }

  private down(socket: Socket | null, reason: string): void {
    if (socket === null || socket !== this.socket) return // a later socket owns us now
    this.socket = null
    const wasLive = this.live
    this.live = false
    this.welcomed = false
    socket.destroy()
    this.failPending(reason)
    if (wasLive || !this.outageLogged) {
      this.outageLogged = true
      this.options.log("warn", `${reason} — retrying every ${RECONNECT_MS / 1000}s`)
    }
    this.schedule()
    if (wasLive) this.options.onHubClosed()
  }

  private failPending(reason: string): void {
    const waiting = [...this.pending.values()]
    this.pending.clear()
    for (const resolve of waiting) {
      resolve({ t: "err", code: "hub_unreachable", detail: reason })
    }
  }

  private schedule(): void {
    if (this.stopped || this.retry !== null) return
    this.retry = setTimeout(() => {
      this.retry = null
      this.open()
    }, RECONNECT_MS)
  }
}

