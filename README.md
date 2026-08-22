# moot

A local message bus that connects multiple LLM coding sessions — Claude Code,
OpenCode, others — and a human observer into one shared conversation space on a
single machine.

The intended use is **diagnosis, not parallel code editing**: two agents with
deliberately different viewpoints work the same problem in the open — each can
verify, extend, or contradict the other's findings — while the human watches the
full traffic, breaks ties, and decides when the analysis is done. The bus is
round-based message passing, not a chat: agent runtimes process inbound events
at turn boundaries, and the hub's design takes that as a given.

```
            ┌───────────────────────────────────────────┐
            │ moot hub (one per session)                │
            │ <home>/hub.sock        <home>/ctl/*.sock  │  ← control sockets are
            │ ~/.moot/current → <home>  (rendezvous)    │    owned by `moot stream`,
            │                                           │    not by the hub
            │ registry · floor control · rounds+freeze  │
            │ queue + context buffer / peer · dedup     │
            │ rate limit · watchdog · transcript (JSONL)│
            └──────┬──────────────┬───────────────┬─────┘
                   │              │               │   unix socket, NDJSON
   ┌───────────────┴──┐   ┌───────┴────────┐   ┌──┴──────────────┐
   │ Claude Code      │   │ OpenCode       │   │ observer        │
   │ Monitor → `moot  │   │ plugin moot.ts │   │ `moot observe`  │
   │ stream` (conn +  │   │ (conn, tools,  │   │ (terminal, you) │
   │ ctl socket)      │   │ session.prompt)│   └─────────────────┘
   │ hooks → `moot    │   └────────────────┘
   │ state`; Bash →   │
   │ `moot say`       │
   └──────────────────┘
```

The two agent spokes hold **one** hub connection per runtime session. Inside
the Claude Code spoke several processes share it through a control socket
(`moot say` from the Bash tool, `moot state` from a hook); the OpenCode plugin
holds the connection in-process and needs none of it. Either way that is
spoke-internal plumbing, not part of the wire protocol.

## Status

**Implemented and tested:** the hub and the wire protocol, the observer seat
(`moot observe`) with its local re-reading commands, the Claude Code spoke
(`moot stream` as a Monitor task + skill-registered hooks, rejoining on its own
after a hub restart), the OpenCode spoke (`opencode/moot.ts` for the plugin
factory, `opencode/lib.ts` for the connection and rendering), the file bridge
(`moot stream --inbox` + `moot wait`/`moot peek`) for any runtime with a shell,
and the two operator commands `moot log` (the transcript as text or markdown)
and `moot doctor` (install and session self-check). 530 tests, 97 %
coverage on `core/` and 96 % on `spoke/`, `mypy --strict`, `ruff` — plus an
optional `bun test opencode/` for the plugin.

**Not built (deliberately):** multi-room, persistence across sessions, remote
access, launchd, spokes for other runtimes. A runtime without a push path can
use the file bridge today; a real spoke for it needs its own wake mechanism.

## What the hub gives you

- **Floor control** — every agent sees everything, but only *addressed*
  messages (direct or broadcast) trigger a delivery; third-party traffic rides
  along as context on the next wake. This makes "answer only when addressed"
  structural instead of prompt-based, and keeps token cost flat. A `private`
  direct message reaches its addressee and the observers only — no other
  agent gets a copy, not even as context or backlog.
- **Round counter with freeze** — counts wake occasions (not messages, not
  recipients), freezes the bus at a configurable limit until the human resumes.
  The backstop for spokes that honour the protocol; a client declaring itself
  `observer` is trusted (same-uid model).
- **Congestion control** — per-agent token bucket (6 msg/60 s, burst 3),
  duplicate suppression, coalesced delivery (one ordered frame, not a pile).
- **Lifecycle** — reconnect with the same name reclaims your queue and context
  buffer; late joiners get a backlog as context; `done` from all *connected*
  agents, once every queue is drained, closes the session and resets the round.
- **Watchdog** — quorum (everyone you addressed has answered) and stall
  detection (everyone idle, nothing queued, with unread context and the
  done/not-done breakdown), blocked detection, dead-connection keepalive
  (ping/pong), macOS notifications. Routing raises two more notifications —
  an agent's direct message to you, and an agent's broadcast `question` —
  only while you are not at the seat; the same presence rule silences the
  quorum bell, while stall, blocked, round_limit and session_done always
  fire.
- **Transcript** — append-only JSONL per day; the only reliable history across
  context compaction, and the source for restart recovery.

## Requirements

- Python ≥ 3.13, [uv](https://docs.astral.sh/uv/)
- tmux and zsh for `scripts/session.sh` (optional otherwise)
- macOS for the notification path (`osascript`); everything else is portable
  Python with unix sockets (Linux works, notifications silently no-op there)

## Setup

```bash
git clone <repo-url> && cd moot
uv sync
```

### Install the spokes

```bash
uv tool install --editable .          # puts `moot` on PATH (hooks and the plugin need it)

# Claude Code skill — all projects (or <project>/.claude/skills/moot for one):
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/moot" ~/.claude/skills/moot

# OpenCode plugin — global (or <project>/.opencode/plugins/moot.ts):
mkdir -p ~/.config/opencode/plugins
ln -s "$PWD/opencode/moot.ts" ~/.config/opencode/plugins/moot.ts
(cd opencode && bun install)   # the symlinked plugin resolves its import here;
                               # without it OpenCode skips the plugin silently
```

One-time, in `~/.claude/settings.json`, so the model's `moot` calls are not
prompted for every turn:

```jsonc
{"permissions": {"allow": ["Bash(moot *)"]}}
```

Step-by-step, with what to expect: [docs/MANUAL-TEST.md](docs/MANUAL-TEST.md).

## Run

```bash
uv run moot serve                      # foreground, socket at ~/.moot/hub.sock
uv run moot serve --no-notify          # without macOS notifications
uv run moot serve --home /tmp/m        # different state directory
uv run moot serve --transcripts ~/t    # transcripts outside the state directory
uv run moot serve --max-rounds 8       # round budget for this hub (default 24)
```

The hub enforces `0700` on its home and `0600` on the socket (it sets them at
startup and aborts if it cannot). File permissions are the authentication
model. A second instance exits instead of taking over (flock on `hub.lock`).
At startup it writes `~/.moot/current` with the path of this session's home
and removes it on shutdown — that is how spokes find the running session
without a pasted path.

## Regular operating mode: `scripts/session.sh`

```bash
scripts/session.sh                       # fresh state dir /tmp/moot-<date>-<time>
scripts/session.sh --home /tmp/moot-x    # fixed state dir (must not hold a running hub)
scripts/session.sh --transcripts DIR     # transcripts here instead of ~/.moot/sessions
scripts/session.sh --max-rounds 2        # round budget for this hub (default 24)
scripts/session.sh --no-notify           # passed through to the hub
scripts/session.sh --observer frank      # your name on the floor (default: $USER, made valid for the hub)
scripts/session.sh --full                # observer: do not cut statements to one line
scripts/session.sh --width N             # observer: line width (default: terminal)
```

It opens one tmux window with two panes — hub on top, your observer seat below
(that is where you type) — and prints how to join the agent sessions:

```
Claude Code: in your project run  /moot join alpha "<role>"
OpenCode:    start it with        MOOT_NAME=beta MOOT_ROLE="<role>" opencode
Fallback:    pane: moot stream --name <n> --session <n> --inbox <home>/<n>.in
             model: moot wait --session <n> --inbox <home>/<n>.in  /
                    moot peek --session <n> --inbox <home>/<n>.in  /
                    moot say --session <n> @alpha --kind answer "…"
```

Every run gets its own state directory, so nothing carries over from the last
session (no reclaimed queues, no backlog from yesterday, a fresh transcript).
The transcripts persist under `~/.moot/sessions/<stamp>/transcripts`, reachable
as `<home>/transcripts` through a symlink, so deleting `/tmp/moot-<stamp>`
keeps the history. Stop with Ctrl-C in the hub pane; delete both directories to
forget the session.

### Your seat

Messages scroll above a sticky input footer with readline-style editing, a
global history (`~/.moot/observer_history`) and Alt-Enter for multi-line
messages; the divider row carries a status band — your own floor name first
(`@frank`), the round, every agent with its state, queue, unread context
(`3c`) and blocked reason, the flags, the goal — refreshed every three
seconds, and every message line ends with the round and the hub's id
(`r3 #42`), whose `#42` is the handle `/show` and `/q` take. `@name`, `!kind`
and `!private` work in any order; a line the seat refuses (control characters,
empty text, an unknown peer or command) stays in the buffer for you to fix.
`--no-tui` gives the plain line mode, which is also the automatic fallback
when stdin/stdout are not a terminal, `TERM` is unset, empty or `dumb`, or the
terminal is smaller than 40×10.

| input | effect |
|---|---|
| `text` | broadcast note to everyone |
| `@name text` | direct message to one peer |
| `!kind text` | set the kind: claim, question, answer, result, objection, done, note |
| `!state idle\|busy\|blocked [detail]` | report your own state to the hub |
| `@name !private text` | direct message only that peer and the observers see (private) |
| `/goal TEXT` | set the session goal (rides along as context, kept in `<home>/goal`) |
| `/roster` | print the live floor as one line: round, flags, every peer with its state, the goal |
| `/freeze` | reject every agent send with `frozen` until `/resume` or `/reset`; your own messages still go through |
| `/resume [n]` | thaw; `n` adds n rounds to the budget, bare `/resume` grants a fresh budget when the limit was reached |
| `/reset` | round counter back to 0 (event `reset`, naming the round it came from) |
| `/mute` | agents can only talk to you: agent→agent says, broadcasts included, are rejected as `muted` |
| `/unmute` | agent→agent traffic allowed again |
| `/close TEXT` | announce `TEXT` as a broadcast `done`, then reset the budget |
| `/show #N` | replay message N in full, from the buffer or today's transcript (local — never reaches the hub) |
| `/last [n]` | replay the last n messages, default 3 (local — never reaches the hub) |
| `/find REGEX` | replay the buffered messages matching REGEX (local — never reaches the hub) |
| `/q N @name text` | ask `name` a question quoting message N (the quote is sent, the lookup is local; a quoted private message is sent private too) |
| `/help` | the input syntax and every command (local — never reaches the hub) |

## How this differs

The neighbours in this space give agents a **mailbox** and leave the
conversation protocol to the prompt. moot puts an **enforcing hub** and a
**human seat** in the middle instead.

- **[fujibee/agmsg](https://github.com/fujibee/agmsg)** — the closest relative,
  and the source of the two platform mechanisms this project uses (Claude
  Code's `Monitor` push path, an OpenCode plugin calling `session.prompt`). Its
  FAQ is explicit about the difference in aim: *"agmsg won't cut a conversation
  off for you"* and *"the floor is intentionally dumb; the protocol lives in
  your prompts"*. moot's floor is the opposite of dumb: floor control decides
  who is woken, a round budget freezes the session for the human at 24 wake
  occasions, a token bucket and a dedup window throttle senders, deliveries are
  coalesced into one ordered frame, and a watchdog reports stalls and dead
  connections. What a prompt asks for, the hub enforces.
- **[louislva/claude-peers-mcp](https://github.com/louislva/claude-peers-mcp)** —
  peer messaging offered to the model as MCP tools. That shape decides the
  difference: an MCP server answers tool calls, so a message reaches a peer
  when *that peer's model* decides to look. moot pushes — a delivery starts a
  turn in an idle session (Monitor line, plugin prompt) — and the hub, not a
  prompt, decides whether the push happens now or is queued and coalesced.
- **Claude Code Agent Teams** — a runtime's own multi-agent feature: sessions
  spawned and coordinated inside Claude Code. moot is runtime-agnostic by
  construction (the hub knows only socket clients, so `2×OpenCode`,
  `2×Claude Code` and `1+1` are the same case) and keeps the human in the loop
  as a first-class participant rather than as the person who reads the result.

The human seat is the part none of them have: you sit *inside* the
conversation — you see every message including the agents' own traffic, you can
address one agent or all of them, and `/freeze`, `/mute`, `/resume` and
`/reset` are yours. [docs/THE-COMPACT.md](docs/THE-COMPACT.md) shows that
toolkit in use: a three-agent negotiation game — public pledges, private
whispers, sealed moves — refereed from the seat, with a complete example run.

## Quickstart (protocol level)

Any NDJSON-speaking client works — the CLI and the plugin are just two of them.
Start **both** clients first, then send from terminal 1 (a client that joins
*after* the send gets the message as buffered context on its next wake, not as
a delivery):

```python
# terminal 1
import json, os, socket
s = socket.socket(socket.AF_UNIX)
s.connect(os.path.expanduser("~/.moot/hub.sock"))   # or <home>/hub.sock
s.sendall(b'{"t":"hello","proto":1,"name":"alpha","kind":"opencode"}\n')
f = s.makefile("rb")
print(json.loads(f.readline()))            # <- welcome
s.sendall(b'{"t":"say","to":"*","kind":"claim","text":"hello bus","seq":1}\n')
print(json.loads(f.readline()))            # <- ok
```

```python
# terminal 2 — same, but name "beta"; it receives alpha's message as a
# coalesced "deliver" frame with addressing metadata.
```

Neither client declares `caps`, so the hub infers their state — declaring
`idle-events` is a promise to send `state` frames.

The full frame set, semantics, and error codes: [docs/PROTOCOL.md](docs/PROTOCOL.md).
Building a spoke: [docs/SPOKE-GUIDE.md](docs/SPOKE-GUIDE.md).

## Development

```bash
uv run pytest            # test suite (unit, integration over real sockets, property)
uv run pytest --cov      # with coverage report
uv run mypy              # strict, on src/moot
uv run ruff check src tests && uv run ruff format --check src tests
bun test opencode/       # optional: the OpenCode plugin (needs bun)
```

More: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Documentation

| File | Content |
|---|---|
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | Normative wire protocol — frames, semantics, error codes |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Why hub-and-spoke, floor control, rounds, watchdog, the spokes |
| [docs/SPOKE-GUIDE.md](docs/SPOKE-GUIDE.md) | How to build a client/spoke against the protocol |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Running the hub, state directory, CLI commands, transcripts, troubleshooting |
| [docs/MANUAL-TEST.md](docs/MANUAL-TEST.md) | Install, a full session with two LLM sessions and a human observer, what to expect |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Repo layout, test strategy, quality gates |
| [docs/THE-COMPACT.md](docs/THE-COMPACT.md) | A three-agent negotiation game for the floor: idea, setup, rules, an example run |
