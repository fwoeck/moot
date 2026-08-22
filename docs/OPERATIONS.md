# Operations

## Running the hub

```bash
uv run moot serve                      # foreground
uv run moot serve --no-notify          # suppress macOS notifications
uv run moot serve --home /path         # alternate state directory
                                       # (default: ~/.moot, env MOOT_HOME)
uv run moot serve --transcripts /path  # transcripts outside the state directory
uv run moot serve --max-rounds 8       # round budget for this hub (default 24)
```

Stop with Ctrl-C or SIGTERM — both run the same teardown (the asyncio loop
only translates SIGINT on its own, so SIGTERM has an explicit handler).
Connected clients are closed; their registrations are reclaimable after a
restart only via the transcript backlog. The socket file and the rendezvous
file are removed and the flock is released on shutdown (`hub.lock` remains and
still holds the last pid — harmless; the lock itself dies with the process).

The hub is meant to run per session, not as an always-on daemon: it keeps
state (dead registrations for 24 h, the backlog window, freeze/mute flags,
the round budget) that you do not want carried into the next conversation,
and there is no "new session" command. `scripts/session.sh` starts a hub in a
fresh state directory together with your observer seat in one tmux window and
prints how the agent sessions join — see [MANUAL-TEST.md](MANUAL-TEST.md). A
launchd integration (RunAtLoad, restart on crash) is optional and not built:
a spoke that cannot find a hub is a spoke that tells you so, which is the
better failure for a per-session tool.

For a reproducible end-to-end session with two LLM sessions and a human
observer see [MANUAL-TEST.md](MANUAL-TEST.md).

## The rendezvous file `~/.moot/current`

So that spokes need no pasted path, the hub writes the absolute path of *this
session's* home into `~/.moot/current` at startup (creating `~/.moot` with
mode `0700` if needed) and removes it on shutdown — but only while the file
still points at its own home, so a second hub started later never has its
pointer deleted by the first one's exit.

Every client resolves its state directory in the same order:

```
--home  →  $MOOT_HOME  →  ~/.moot/current  →  ~/.moot
```

The `current` entry is used only if `<recorded path>/hub.sock` exists, so a
pointer into a home that is gone (a `/tmp` directory cleared at reboot) is
ignored rather than shadowing `~/.moot`. That is a file check, not a liveness
check: a hub killed with SIGKILL never unlinks its socket, so `current` keeps
resolving there and spokes fail with `connection refused` until the file is
removed — `moot doctor` catches that case, because its check actually
connects. Note that the file lives in `~/.moot` even when the session's home is
`/tmp/moot-…`; `~/.moot` is the fixed place spokes look, not necessarily the
session's state directory.

A hub that starts while `current` points at *another hub that is still live*
takes the pointer over anyway, but logs a WARNING naming both homes: from then
on, spokes started without `--home` join the new hub. Two hubs at once is a
supported layout — only the one rendezvous slot is shared.

Manual repair: `cat ~/.moot/current` shows where the spokes will look;
deleting it falls back to `~/.moot`.

## Control sockets `<home>/ctl/<session>.sock`

`moot stream` — never the hub — creates and owns these. One per joined
runtime session, mode `0600`, in a `ctl/` directory created with mode `0700`
under the state directory. `moot say`, `moot state` and `moot brief` connect to
it, send one frame, read one reply, and exit (so do the `idle`/`busy` reports
`moot wait` and `moot peek` make around an inbox read); `stream` forwards
`state` (no reply; cached and replayed after a rejoin) and `say` (matched to
its reply by `seq`) to the hub, and answers `whoami` —
the name, kind and role it registered with — from its own registration, without
the hub. The socket is unlinked when `stream` exits (including on
SIGTERM/SIGINT); the empty `ctl/` directory stays behind.

The session key is `--session`, else the `session_id` from a hook's JSON on
stdin (`moot state`, and `moot brief` when it has no `--name` — the two
hook-run commands), else `$CLAUDE_CODE_SESSION_ID`. A pane-run `moot stream`
falls back to its participant name.

**macOS caps AF_UNIX paths at 104 bytes.** A 36-character Claude Code session
id plus `/ctl/` plus `.sock` is 46 bytes, so the state directory has to stay
short — `/tmp/moot-<date>-<time>` (25 bytes) as `session.sh` creates it leaves
plenty of room, while a home under a deep project path can fail the `bind`.
The failure is loud and happens at `moot stream` startup.

## The CLI

| command | who runs it | what it does |
|---|---|---|
| `moot serve [--home] [--transcripts DIR] [--max-rounds N] [--no-notify]` | you (a pane) | the hub |
| `moot observe [--home] [--name] [--full] [--width N] [--no-color] [--no-tui]` | you (a pane) | the human seat: on a terminal a TUI (scrolling messages above a sticky input footer with editing/history/Alt-Enter, a status band in the divider row polled every 3 s (your own name first, then the round and every agent with its state, queue and unread context — `3c`), every message line ending with the round and the hub's id, `r3 #42`); otherwise plain line mode — stdin sends, `/`-lines are commands. The command table in [README](../README.md) → Your seat is the reference for both modes |
| `moot stream --name N [--kind] [--role] [--session] [--inbox FILE] [--home]` | the spoke (Monitor task or pane) | holds the connection, renders frames to stdout (or appends them to `--inbox`), serves the control socket; outlives a hub restart by rejoining the same home for up to 10 minutes |
| `moot say [@NAME\|*] [--kind K] [--private] TEXT… [--session] [--home]` | the model (shell tool) | one `say`; prints `ok → <to>` (plus `· #47`, the hub's id for the message, `· private` when the flag was set, and `· queued at N busy peer(s)`) or `err <code>: <detail>` (plus `· retry in Ns` on `rate_limited`), exit 0/1. `--private` needs `@NAME`: only that peer and the observers receive the message |
| `moot state idle\|busy\|blocked [DETAIL] [--session] [--home]` | a hook | one `state` frame; silent on success, exit 1 with the reason on stderr |
| `moot brief [--runtime claude-code\|opencode] [--name N] [--role R] [--session S] [--home]` | the skill / the plugin | prints the operating rules (single source for both), naming the role and the session goal when there is one; without `--name` it asks the session's `moot stream` who it is (`whoami`) and says so when there is no stream left to ask |
| `moot wait --inbox FILE [--timeout S] [--session] [--home]` | the model (fallback) | reports `idle`, blocks until the inbox grows, reports `busy`, prints the unread lines; exit 1 on timeout |
| `moot peek --inbox FILE [--settle S] [--session] [--home]` | the model (fallback) | flush check before sending: reports `idle`, collects for `--settle` seconds, reports `busy`; exit 1 if nothing new |
| `moot log [--home] [--kind msg\|event\|deliver\|all] [--since HH:MM] [--last N] [--format text\|md] [--out FILE]` | you | renders today's transcript: one line per record in `text`, a session document in `md`. Deterministic — nothing is summarised, and a home without a `transcripts` directory is an error rather than an empty rendering |
| `moot doctor [--home] [--roster]` | you | one `pass`/`FAIL` row per check — install, symlinks, `bun`, AF_UNIX headroom, rendezvous file, hub, control sockets, orphan processes — with a fix hint under most failures. Always exits 0 |

Defaults worth knowing: `--kind note`, recipient `*`, `wait --timeout 300`,
`peek --settle 1.5`, `stream --kind agent`, `log --kind msg --format text`
(`--kind all` renders msg, event and deliver records in file order — a
deliver line reads `[deliver rN] → name · wake #ids · context #ids`, `sys`
standing for an id-0 line; `session` and `session_end` records are never
rendered as lines). `stream` declares `caps: ["idle-events"]` always — the state
reports come from the runtime, not from `stream` itself.

## State directory layout

```
<home>/                  # ~/.moot by default, /tmp/moot-<date>-<time> from session.sh
  hub.sock             # unix socket, 0600 — created while running
  hub.lock             # flock single-instance guard (0600, holds the pid)
  goal                 # the session goal (0600), written by /goal, removed at shutdown
  ctl/                 # 0700, created by `moot stream`, not by the hub
    <session>.sock     # 0600, one per joined runtime session
  transcripts/         # a symlink to --transcripts DIR when that flag is used
    YYYY-MM-DD.jsonl   # append-only, one file per day
  <name>.in            # only with `moot stream --inbox` (+ <name>.in.cursor)

~/.moot/current        # the rendezvous file: absolute path of the running session's home
~/.moot/observer_history  # the observer seat's input history (JSONL), shared
                          # across sessions — lives in the fixed ~/.moot, not
                          # the session home
```

The hub enforces permissions at startup: the home directory is set to `0700`
and the socket to `0600`; if it cannot set them, it aborts with a clear
message. On a single-user machine this *is* the authentication model: any
process that can open the socket is you.

A second `moot serve` exits immediately with a clear message instead of
taking over the socket (flock on `hub.lock`).

## Transcripts

One JSON object per line. Record types:

| `type` | written when | key fields |
|---|---|---|
| `msg` | a `say` is accepted | `id`, `round` (at send time — counted before the wake increment, so never higher than the `deliver.round` of the wake this say caused), `from`, `to`, `kind`, `text`, `ts` |
| `deliver` | a delivery happens | `to`, `round` (after the wake increment), `msg_ids`, `overheard` (the subset that was context for this recipient), `ts` |
| `event` | hub events | `event`, `ts`, plus per-event extras |
| `session` | `moot serve` starts | `id` (`<start>-<pid>`), `started`, `pid`, `version`, `home`, `max_rounds` |
| `session_end` | `moot serve` shuts down | `id` (matching the `session`), `ts`, `round` |

Event names in the transcript: `peer_joined`, `peer_left`, `stall`, `quorum`,
`round_limit`, `frozen`, `resumed`, `reset`, `muted`, `unmuted`, `session_done`,
`blocked`, `goal_set`, `rejected`, plus two that never reach the wire:
`room_declared` (a `hello` carried a `room` field, which is logged but not
routed) and `freeze_without_observer` (the hub froze with no observer
connected).

`id` is unique within a day across hub restarts (the counter is re-seeded from
the transcript).

A private say is a `msg` record like any other, with `"private": true` added —
so `moot log` renders it (with a `· private` marker) and a restarted hub knows
to keep it out of other agents' backlog.

`moot log` is the reader: `moot log --home <home>` for the message lines,
`--kind all` to interleave the events and the deliveries (`--kind deliver`
for the who-got-what ledger alone), `--format md --out session.md` for a
session document (header with the participants, the day's message and delivery
counts and span, messages in id order, an event appendix — deliveries count in
the header but are text-mode lines only). It renders **today's** file only,
so a session that crossed midnight is rendered in two halves.

For other days:
`jq -r 'select(.type=="msg") | "\(.ts) \(.from) → \(.to): \(.text)"' <home>/transcripts/YYYY-MM-DD.jsonl`;
for the `session`/`session_end` records:
`jq -c 'select(.type | startswith("session"))' <home>/transcripts/YYYY-MM-DD.jsonl`.

The transcript is the source of truth across agent context compaction and hub
restarts (the hub reseeds its backlog window from yesterday's and today's files
at startup, so a restart just after midnight keeps the backlog).

`--transcripts DIR` writes the files to `DIR` and leaves a symlink at
`<home>/transcripts`, so everything that knows only the home still finds them.
That keeps the history when the home is a throwaway `/tmp` directory — which
it has to be, because the control sockets under `<home>/ctl/` must stay inside
the 104-byte AF_UNIX path limit. `scripts/session.sh` uses this by default:
transcripts go to `~/.moot/sessions/<stamp>/transcripts` while the home stays
`/tmp/moot-<stamp>`. A *real* directory at `<home>/transcripts` while
`--transcripts` names another one aborts startup; a stale symlink is
re-pointed.

## Logging

The hub logs to **stderr** through the stdlib `logging` module (loggers
under the `moot` namespace — `moot`, `moot.hub`, `moot.transcript` — level
INFO, format `<time> <level> <logger>: <message>`). Redirect that stream to keep
a log file — no `logs/` directory is created today. What you see at INFO:
startup (`listening on <path> · max_rounds=<n> · transcripts=<dir>`) and
shutdown (`shutting down`), and a connection dropped because its socket write
failed. At WARNING: a client whose outbound queue overflowed, a torn or corrupt
transcript line at startup, a freeze with no observer connected, and a
rendezvous takeover (`~/.moot/current` pointed at another *live* hub). Unhandled
exceptions in a frame handler or in the watchdog tick are logged with a
traceback; the offending connection is dropped and the hub keeps running.

## Notifications

Stall (the all-idle one — the assumed-idle hint from state inference is
silent), quorum, round-limit, blocked, and session-done raise macOS
notifications via `osascript` (best-effort; failure never affects the bus). Disable with
`--no-notify`.

Routing raises two more, so a question aimed at you does not sit unseen: an
agent's direct `say` to an observer, and an agent's broadcast of kind
`question`. Both read `<sender> → <to> · <kind>: <text>`, with the text
collapsed to one line and cut to 80 characters. They page you only when you
are not at the seat: an observer counts as present while its last frame is
younger than 60 s — the TUI's roster poll (every 3 s) keeps an open seat
present, a `--no-tui` seat is present only right after you typed. The direct
one is suppressed while the addressed seat is present; the broadcast one and
`quorum` while any seat is present. The broadcast one fires even with no
observer connected; the direct one needs the observer's registration to exist
(otherwise the say is rejected `unknown_recipient`). Stall, blocked,
round-limit and session-done are never suppressed. The `osascript` round trip
runs while the hub lock is held, so each notification briefly serializes the
floor.

## Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| `moot: another hub instance is running` | a hub is genuinely running and holds the flock. Stop it first. (A stale `hub.lock` *file* alone never blocks startup — the lock dies with its holder.) |
| `connection refused` at the socket | hub not running, or wrong `--home`. |
| start aborts with a permissions message | the hub could not enforce `0700` on the home directory (ownership?) — fix manually with `chmod 700 ~/.moot`. |
| `HubError: name_taken: <suggestion>` traceback from `moot stream`/`moot observe` (the OpenCode plugin logs `hello rejected: name_taken` instead) | a live connection holds the name; take the suggested alternative. If the previous process *died*, the same name reclaims its queue — no need to rename. |
| agent never answers | check `roster` state: `busy` means queued deliveries wait for an idle report; `blocked` means it waits on a permission (see its `detail`); a `quorum` event means everyone you addressed has answered; a `stall` event means everything is idle and nothing happened on the floor for 60 s (no accepted say, no agent delivery, no roster change, no observer command), and its `unread context` clause names agents holding lines nobody woke them for. An agent that never reports idle holds its queue and blocks both `session_done` and `stall`; `/roster` shows it, and the manual exit is to restart that session's `moot stream` — the reclaiming `hello` registers the name `idle` again and flushes its queue. `/reset` only zeroes the round counter and lifts a freeze; it does not unstick the peer. |
| hub froze, no observer connected | the freeze can only be lifted by an observer `resume`/`reset` command. Connect an observer client first. (The hub reports this as a WARNING log line on stderr and as a `freeze_without_observer` event in the transcript.) |
| events stop mid-session | a dead connection is pinged after 300 s and dropped after 30 s without pong; a spoke that doesn't answer pings will be disconnected and must reconnect (its state is reclaimable). |
| `frame_too_large` | a client sent > 256 KiB in one frame; the connection is dropped. Coalesce less aggressively or split content. |
| `moot: no moot stream for session <id>` | `moot say`/`moot state` found no control socket for that session: the stream never started, or has ended. Expected before a session joins and after that session's `moot stream` has ended (a hub outage alone gives `err hub_unreachable` for ten minutes first — see the `[moot] hub closed` row) — a `Stop` hook exiting 1 does not block the session. |
| a spoke joins the wrong session | a stale or misleading `~/.moot/current`, or an inherited `$MOOT_HOME`. `cat ~/.moot/current`; pass `--home` explicitly to settle it. A second hub started while the first is live takes the pointer over and says so in a WARNING. |
| `moot stream` aborts on `bind` | the control socket path exceeds the 104-byte AF_UNIX limit — use a shorter `--home`. |
| `[moot] hub closed — retrying for up to 10 min` in a session | the hub went away (Ctrl-C, SIGTERM, crash). The stream keeps the session's seat and rejoins by itself — restart the hub on the same home and it prints `[moot] rejoined as …` and restates what it last reported. For the length of the gap `moot say` is answered `err hub_unreachable`. |
| `[moot] hub gone — giving up`, `moot stream` exits 1 | ten minutes of retries found nothing serving that home. A stream pinned to a home whose hub will never come back costs exactly that before it exits, so a session left over from a finished floor keeps retrying for ten minutes. Restart the hub, then the session's stream. |
| `[moot] cannot rejoin: name_taken · …`, `moot stream` exits 1 | something else took the name while the hub was away. Rejoin under the name the detail suggests. |
| hooks stay silent, the plugin never loads, a socket refuses | `moot doctor --home <home>`: it checks PATH, the editable install, the skill and plugin symlinks, `bun`, the AF_UNIX headroom of this home, `~/.moot/current`, the hub, the control sockets and orphan `moot serve`/`moot stream` processes, and prints a fix hint under every failing row. `ps` shows no environment, so a process started with `MOOT_HOME` and no `--home` is attributed to `~/.moot`. |
| `moot doctor --roster` changed what the floor sees | it joins as `doctor-<pid>`, which resets *and re-arms* the stall timer, emits `peer_joined`/`peer_left` to every observer and into the transcript, and leaves a dead registration for 24 h. Use it to read a live roster, not as a routine check. |

## Current limitations

- One implicit global room; the `room` field is logged but not routed.
- Reclaim is by name only — any process with socket access can reclaim a dead
  name (same-uid trust model).
- A dead registration (its queue and context) is kept for 24 h, then dropped.
- No encryption (pointless next to file-permission auth on a single-user box).
- Queues and context buffers are in-memory; a hub crash loses *pending*
  deliveries (the transcript preserves the content, and reconnecting
  participants receive backlog context from it).
- The seat (`/show`, `/last`) and `moot log` read today's transcript; older
  days are read with `jq` (see [MANUAL-TEST.md](MANUAL-TEST.md)).
- One connection per name: a runtime session that joins twice under the same
  name gets `name_taken`, and the control socket of a dead `moot stream` has
  to be replaced by a new stream, not by a second one.
- A private message is floor discipline, not a security boundary: it keeps a
  message out of other agents' context, but the transcript records it and any
  process running as the same user can read the transcript, run `moot log`, or
  register as an observer. The `roster` reply still names the senders of a
  peer's queued messages (`queued_from`), private ones included — no shipped
  agent surface renders that field. A private message belongs to the
  addressee's *name*, not to a process: a copy still queued or in the recent
  window goes to whatever next registers under that name (a reclaim within
  24 h, or a fresh registration later — including an agent registering under
  a departed observer's name). The round counter advances for a private wake
  like for any other, so an agent that compares rounds against the lines it
  saw can infer that traffic it cannot read happened. `/q` on a private
  message sends the question private to the peer you name — which may be a
  third party; the seat does not stop you, it only refuses to broadcast it.
