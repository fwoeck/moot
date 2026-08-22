# Architecture

## Hub-and-spoke, and why the hub knows nothing about participant types

The hub holds no outgoing connection to any agent runtime. Every participant —
a Claude Code `Monitor` task, an OpenCode plugin, a human's terminal — connects
*inward* to the hub's unix socket and speaks the same NDJSON protocol. From the
hub's perspective there is exactly one kind of thing: a socket client.

Consequence: there are no adapter classes and no per-runtime code paths in the
hub. All type-specific logic lives in the spoke. `2×OpenCode`, `2×Claude Code`,
and `1+1` are the same case and cost zero hub complexity. A future participant
(Codex, Aider, a CI bot, a second human) needs a new spoke, never a hub change.

Runtimes differ in what they can report, and that difference is handled by
**capability negotiation**, not inheritance: participants declaring
`caps: ["idle-events"]` are trusted to send `state` frames. Every agent
recipient is marked busy on delivery; for participants without the capability
the hub additionally infers (their say → idle; 90 s without a say after going
non-idle → idle). Both shipped agent spokes declare the capability, so
inference is a fallback for third-party clients rather than the normal case.

## Round-based message passing, not chat

Agent runtimes do not process inbound events mid-turn: events queue and are
handled at the next turn boundary, batched (measured behavior: a push during a
long generation is processed only when the turn ends; a push while the runtime
merely *waits* on a background task is processed promptly). There is no
interrupt. The bus embraces this instead of fighting it — deliveries are
coalesced, ordered batches, and congestion control is hub-side rather than
prompt-side.

## Spokes: how a delivery becomes a turn

The hub can only queue and send; something on the other side has to turn a
frame into a turn of the model, and hand the model's answer back. That is the
spoke, and each runtime offers a different mechanism for it.

**Claude Code — push by Monitor, state by hooks.** The skill
(`/moot join alpha`) starts one persistent `Monitor` task running `moot stream`.
Every stdout line of that command arrives in the session as a notification and
starts a turn, even from the idle prompt — so a rendered `deliver` line *is*
the wake. The same skill's frontmatter registers hooks for the rest of the
session: `Stop` → `moot state idle`, `UserPromptSubmit` → `moot state busy`,
and `SessionStart(compact)` → `moot brief`, because a compaction drops the
skill body but not the Monitor task.

**OpenCode — push by prompt, state by events.** The plugin holds the socket
inside the OpenCode process. A delivery becomes
`client.session.prompt(...)` on the session that most recently finished a
turn; `session.idle` and `session.status {busy}` become `state` frames. The
prompt call is deliberately fire-and-forget: it resolves only when the model's
reply is complete, so awaiting it would park the hub reader for a whole turn.

**Any runtime with a shell — the file bridge.** `moot stream --inbox FILE`
writes the same rendered frames into a file (stream notices stay on stdout);
the model reads them with `moot wait`, which reports `idle` on entry (the hub
then flushes its queue) and `busy` before it hands lines back — a timed-out
`wait` stays idle. No push path, but the same protocol behaviour.

**Why the turn boundary is reported, not inferred.** The hub's fallback
inference (90 s without a say means idle) is a guess that is wrong in both
directions: a model thinking for three minutes looks idle, and a model that
finished in five seconds keeps its queue waiting. Both shipped spokes sit
*inside* the runtime's own lifecycle — a hook that fires exactly when the turn
ends, an event that fires exactly when the session goes idle — so the hub gets
the truth for free and coalescing lands on real boundaries. This is why the
`state` frame exists at all, and why `caps` is a promise rather than a hint.

**One name, one connection.** A protocol name may hold exactly one connection,
but a Claude Code spoke is several processes (the Monitor task, one `moot say`
per tool call, one `moot state` per hook, one `moot brief` per compaction).
They share the single connection through a control socket that `moot stream`
serves at `<home>/ctl/<session>.sock`, taking `say`, `state` and `whoami` —
spoke-internal plumbing the hub knows nothing about (see
[SPOKE-GUIDE.md](SPOKE-GUIDE.md)). The alternative, a protocol
frame for "second connection acting for a name", was considered and rejected:
it would put a spoke's process model into the wire contract.

## Floor control

The core congestion decision: every agent sees everything, but only addressed
messages trigger a delivery.

- **Wake deliveries** (`direct`, `broadcast`) go to idle agents immediately and
  start a turn; for busy or blocked agents they queue.
- **Context** (`overheard` — traffic between others) accumulates in a
  per-participant buffer and rides along on the next wake delivery, never
  alone.
- **Private** (`say.private`) is a direct wake with no third-party copy: the
  addressee gets it, every observer gets it, no other agent ever does — not
  as context, not as late-join backlog, not after a hub restart. It is floor
  discipline, not secrecy: the point is to keep a message out of another
  model's context window while the human still audits everything, and the
  transcript records it like any other say.

Broadcasting everything to everyone would triple token cost for information
two-thirds of the recipients only need to know, and worse: a model that is
woken tends to answer. "Answer only when addressed" is unreliable as a prompt
rule precisely when the model is already running; the wake/context split makes
it structural.

Coalescing is the second lever: instead of N independent events with no visible
causality, the hub sends one `deliver` frame with a list ordered by message id
— the model sees a history, not a heap.

Caps keep buffers honest: context buffer 15 (FIFO eviction with a placeholder
line), wake queue 100 (drop-oldest, placeholder line), at most 10 queued wake
messages per delivery (the rest stays queued).

## Rounds and freeze

A round is a *wake occasion*, not a message and not a recipient: +1 per `say`
that wakes at least one agent, +1 per queue flush on an idle transition (a
flush that carries only `system` notes does not count). This keeps the freeze
threshold independent of participant count — a broadcast to three agents is one
escalation step, not three.

At `max_rounds` (default 24, set per hub with `moot serve --max-rounds N`) the
hub freezes: agent says are rejected, observer traffic continues, and only the
human can thaw (`resume`/`reset`). This is the backstop for spokes that honour
the protocol; a client declaring itself `observer` is trusted (same-uid model).
If no observer is connected at freeze time, the hub logs a warning — a frozen
hub without an observer cannot be thawed.

## Watchdog

A 5 s tick checks one success and three failure modes (listed by how often
they fire, not in the order the tick evaluates them):

1. **Quorum** — every agent the observer's last say addressed has answered,
   everyone is idle, nothing is queued: the "you can speak now" bell on a
   moderated floor, once per observer say that addresses at least one agent.
   It also raises a macOS notification, but only when no observer is present
   (the presence rule below).
2. **Stall** — all agents idle, all queues empty, no activity for 60 s. The
   most likely failure mode in practice: two agents silently waiting for each
   other. Fires once per quiet period, plus a macOS notification. Its `detail`
   names the unread context per agent (buffered lines nobody was woken for —
   the gate ignores them on purpose) and splits the agents into `done` and
      `not done`; after a `session_done`, and until the next accepted agent say,
   it names when the session finished instead of the done/not-done split.
3. **Blocked** — an agent reports `blocked` for over 60 s; the notification
   carries the `detail` (e.g. which permission prompt is waiting).
4. **Dead connection** — 300 s of silence triggers a `ping`; no `pong` (or any
   other frame) within 30 s closes the connection. Because an unaddressed
   agent is *routinely* silent for minutes, the keepalive runs in steady state,
   not as an exception.

Routing notifies too, outside the watchdog: an agent's direct `say` to an
observer and an agent's broadcast `question` raise a macOS notification when
the human is not at a seat — the direct one only while the *addressed*
observer is absent, the broadcast one while no connected observer is present
(presence = a frame from that observer within 60 s; the TUI's roster poll
counts) — so a question aimed at the human reaches them without an open seat
and does not page them while they are reading it.

## Registry, reconnect, restart

Names are claimed by `hello`. A name held by a live connection is rejected
with a suggested alternative; a name left by a dead connection is reclaimed —
including its waiting queue and context buffer, so a restarted agent process
resumes without loss. Reclaim treats the newcomer as a fresh process: the
`finished` mark and any blocked bookkeeping are reset, and the inherited queue
is delivered immediately. A dead registration is kept for 24 h, then dropped.

The hub itself is designed to be restartable: registry and buffers are
in-memory, but the append-only transcript survives. On startup the hub reseeds
its recent-message window from yesterday's and today's transcripts (a restart
just after midnight keeps the backlog), so a participant reconnecting after a
hub crash still receives backlog context (the same mechanism as late join).

The spoke side does the reconnecting, so a hub restart costs a connection
rather than a session: `moot stream` outlives the hub it joined. Its reader
thread supervises the connection — on EOF it keeps serving the control socket
the hooks use, retries the same home at 1 s, 2 s, then 5 s for at most ten
minutes, and on the new `welcome` restates the state it last reported (a
reclaim starts the registration at `idle`). A `say` attempted during the gap
is answered `hub_unreachable` instead of parking the model; a refused `hello`
and a spent budget both end the stream with rc 1, and a frame the renderer
cannot handle still takes the process down at once — that is a spoke bug, not
an outage.

## Transcript

`<home>/transcripts/YYYY-MM-DD.jsonl`, append-only. Record types: `msg`
(every accepted say, with the round at send time), `deliver` (every delivery,
with recipient, message ids and which of them were overheard context; its
`round` is counted after the wake, `msg.round` before it), `event`, and the
pair `session` / `session_end` that brackets one `moot serve` run with its pid,
version, home and round budget. `moot serve --transcripts DIR` moves the files
out of the state directory and leaves a symlink behind, so a throwaway `/tmp`
home (short enough for the AF_UNIX control sockets) does not cost the history.
Agent context erodes under compaction; the transcript is the only reliable
history and the basis for restart recovery, `moot log`, and post-hoc analysis.

## Deployment choices (and what was rejected)

- **Unix socket, not TCP**: file permissions (`0700` home, `0600` socket) are
  the authentication model. On TCP, auth would have to be built (tokens), for
  zero benefit on a single-user machine.
- **No containers**: a unix socket created inside a container on macOS does not
  work across the VM boundary — socket *files* propagate through the bind
  mount, but `connect()` fails with `ECONNREFUSED` in both directions (measured
  on OrbStack; consistent with the long-known Docker Desktop limitation). The
  spokes must run natively anyway (they are spawned by/in the agent runtimes).
  The core avoids macOS-specific APIs outside the notification module, so a
  Linux variant stays possible.
- **Per-session hub, not an always-on daemon.** `moot serve` runs in
  the foreground with its own state directory per session, and the
  transcripts can live outside that directory (see Transcript above), so a
  throwaway home costs no history. `scripts/session.sh` opens a tmux window
  with the hub and the observer seat; the agent sessions join themselves. A
  launchd integration (RunAtLoad, restart on crash, logs to `~/.moot/logs/`)
  is deliberately not built — an always-on hub would carry dead
  registrations, backlog and freeze/mute state between unrelated
  conversations.
- **A rendezvous file, not a discovery protocol.** Spokes start in arbitrary
  directories and cannot be handed a path by the human every time. The hub
  writes its home into `~/.moot/current` at startup and removes it at
  shutdown; a spoke reads it and ignores it when the recorded home has no
  socket. One file, no broadcast, no daemon registry — and because there is
  only one slot, a hub starting while another is live takes the pointer over
  and logs a WARNING naming both homes.

## Module map

| Module | Responsibility |
|---|---|
| `core/proto.py` | frame validation, error codes, encoding |
| `core/types.py` | internal message representation (`Msg`), the `Client` transport contract, and wire conversion |
| `core/hub.py` | the state machine: floor control, rounds, freeze, mute, lifecycle, watchdog |
| `core/server.py` | asyncio wiring: socket, framing, permissions, lockfile |
| `core/buffer.py` | context buffer with eviction placeholder |
| `core/bucket.py` | token bucket |
| `core/dedup.py` | duplicate window |
| `core/transcript.py` | append-only log + restart reseed |
| `core/notify.py` | macOS notifications (injectable) |
| `core/clock.py` | injectable time source |
| `core/config.py` | all tunables in one place |
| `spoke/conn.py` | blocking client side of the wire protocol |
| `spoke/home.py` | state-directory resolution + the rendezvous file |
| `spoke/render.py` | inbound frames as text for a model (pinned by fixture) |
| `spoke/ctl.py` | the control socket, both ends |
| `spoke/brief.py` | the operating rules, as data |
| `spoke/observer.py` | the human seat (seat state, local commands, status band, plain line mode, TUI wiring) |
| `spoke/tui.py` | the seat's terminal kit: scroll region + sticky footer |
| `cli/__init__.py` | the `moot` command: argparse and dispatch for serve, observe, stream, say, state, brief, log, doctor, wait, peek |
| `cli/stream.py` | the process that owns one session's connection |
| `cli/inbox.py` | `wait`/`peek`, the file bridge |
| `cli/log.py` | today's transcript as text or markdown, deterministically |
| `cli/doctor.py` | install and session self-check, one row per check |
| `opencode/moot.ts` | the OpenCode spoke's plugin factory: events, the `moot_say` tool, prompt injection |
| `opencode/lib.ts` | the spoke's connection, renderer (fixture-pinned) and home resolution |

The hub logic is deliberately asyncio-free: the server layer moves bytes, the
hub moves state. Tests drive the hub either directly (fake clients) or through
real sockets.
