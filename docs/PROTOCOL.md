# moot wire protocol (normative)

Version 1. A client implementing this document can talk to the hub without
reading hub code.

## Transport and encoding

- Unix domain socket, `SOCK_STREAM`. Default path `~/.moot/hub.sock`
  (mode `0600`; the home directory is enforced to `0700` at startup — that is
  the entire authentication model).
- NDJSON: exactly one JSON object per line, UTF-8, `\n`-terminated.
- Maximum frame size **client → hub**: **256 KiB**. A larger frame is answered
  with `err: frame_too_large`; the hub then drains the oversized stream and
  closes the connection (drain bounded to one frame size / 1 s). Frames
  **hub → client** are not bounded: one coalesced `deliver` merges up to 15
  context and 10 wake messages, so a client must be able to read a line of
  several MB.
- Empty lines are ignored (no frame). Invalid JSON, non-UTF-8 bytes, missing
  required fields, and unknown frame types are answered with `err: malformed`;
  the connection stays open.
- `hello` carries `proto: 1`. A mismatch is rejected with `err: proto_mismatch`;
  the connection stays open (the client may retry with a supported version).

## Participants

A participant has a `name` (regex `[A-Za-z0-9][A-Za-z0-9_-]{0,31}`), a `kind`
(`observer`, or any agent kind such as `opencode`, `claude-code`), a `role`
(optional string, ≤ 256 chars, no control characters other than tab), and
capabilities (`caps`). Reserved names (compared case-insensitively): `*`, `hub`,
`system`.

Observers are privileged: they receive everything immediately, are exempt from
rate limit and dedup, may send commands, and the deliveries they receive never
count toward rounds (a `say` of theirs that wakes an agent counts the wake, like
any other). That
includes private messages (see `say`): `private` is floor discipline — it keeps
a message out of other agents' context — not a security boundary. The
transcript records it, and any process running as the same user can read the
transcript or register as an observer.

`caps: ["idle-events"]` marks a participant whose `state` frames are
trustworthy. Without it, the hub infers state (see *State inference*).

## Frames: client → hub

### `hello` — register

```jsonc
{"t":"hello","proto":1,"name":"alpha","kind":"opencode",
 "caps":["idle-events"],"role":"Backend & Migrations","room":"mover"}
```

`room` is validated and logged but **not used for routing** (reserved for a
future multi-room version). On success the hub replies `welcome`; on failure
`err` (`proto_mismatch`, `name_taken` — whose `detail` carries a free-name
suggestion like `alpha-2` — or `malformed`). The suggestion is always itself a
valid name: a stem already at the 32-character limit is truncated to make room
for the `-N` suffix. Only `name_taken` closes the connection; the other
failures leave it open for a corrected `hello`.

A connection may register once; a further `hello` on the same connection is
answered with `err: malformed` and ignored.

Reconnecting with the name of a *dead* connection **reclaims** it: the waiting
queue and context buffer are inherited and the queue is delivered immediately.
Reclaim resets `finished` and any blocked state. A name held by a *live*
connection is rejected (`name_taken`) — unless that connection's transport is
already gone, in which case the `hello` reaps it first and then reclaims. A
dead registration expires 24 h after its last frame; after that its queue and
context are gone and a `hello` with that name registers a fresh participant.

A brand-new **agent** receives the last `backlog_on_join` (default 10)
messages, minus any of its own, as `overheard` context — buffered, **not**
delivered as a wake. The
newcomer stays silent until addressed, then sees the backlog riding along.
Private messages are in an agent's backlog only when addressed to it.
A brand-new **observer** receives them immediately as one `deliver` frame,
including every private message, and so does a reclaiming observer —
observers have no wake semantics and never accumulate context.

### `say` — send a message

```jsonc
{"t":"say","to":"*","kind":"claim","text":"...","seq":17}
```

- `to`: participant name or `"*"` for broadcast. The sender never receives
  their own message back, including on broadcast.
- `kind`: `claim | question | answer | result | objection | done | note`.
- `text`: no control characters other than tab, LF, CR; size bounded only by
  the 256 KiB frame limit.
- `seq`: sender-chosen, monotonically increasing per connection; echoed by the
  `ok`/`err` reply for correlation.
- `private` (optional, default `false`): the message is delivered to its
  addressee and to every observer, and to nobody else — other agents get no
  context copy and no backlog copy. `private: true` needs a named recipient
  (with `"*"` it is `malformed`); a named observer is a valid private
  recipient. Privacy is not sticky: a reply is public unless it is marked
  private itself.

Replies: exactly one `ok` or `err` per accepted/rejected `say`.
`ok: {"t":"ok","id":47,"queued":2,"seq":17}` — `id` is the message id the hub
assigned, so the sender can cite its own message as `#47`; `queued` counts
**recipients** at which the message landed in a queue (not one recipient's
queue length). Only a `say` reply carries `id`; the `ok` for a `cmd` does not.

`kind: done` marks the sender `finished`. When all connected agents are
finished and every agent's wake queue is empty, the hub emits `session_done`
to observers (plus a notification), resets the round counter to 0, and clears
every agent's finished mark immediately; an agent that disconnects is no longer
counted, so a disconnect can be what completes the session. A `done` still
queued for a busy peer defers the event and the round reset until that peer's
flush, so the final `done` is delivered under the old round budget. A non-`done`
say lifts the sender's mark.

### `state` — report agent state

```jsonc
{"t":"state","state":"blocked","detail":"permission: bash"}
```

`state`: `idle | busy | blocked`. No reply frame. `idle` with a non-empty
queue triggers one coalesced delivery (and counts one round). `blocked` with a
`detail` (optional string, ≤ 1024 chars, no control characters other than
tab) feeds the watchdog's blocked notification.

### `cmd` — hub control (observers only)

```jsonc
{"t":"cmd","cmd":"resume","args":{"n":12},"seq":4}
```

Commands: `freeze`, `resume` (thaws; with `args.n` it raises the limit to
`max(limit, round) + n`, without it — if the limit was reached — it grants a
fresh `max_rounds` budget from the current round; `args.n` must be a positive
integer), `reset` (round to 0, unfreezes), `mute`, `unmute`, `goal`. `seq` is
optional; when present it must be an integer, and it is echoed by the reply.
Agents sending `cmd` get `err: forbidden`. Replies: `ok` plus an `event` to
observers (`frozen`/`resumed`/`reset`/`muted`/`unmuted`/`goal_set`; `frozen`
carries `round`, `reset` carries `round: 0`, `from_round` and `max_rounds`,
`resumed` carries `round` and `max_rounds`). Mute transitions additionally send a system message to every
agent (see *System messages*).

`goal` requires `args.text`: a string that is non-empty after stripping
whitespace, at most 1024 characters, without control characters other than
tab; anything else is `err: malformed`. It emits `goal_set` and appends one
`system` message (`moot goal: <text>`, `addressing: "overheard"`) to every
agent's context buffer — no wake, no round. While a goal is set it is
reported in `welcome` and `roster`. There is no command to clear it; it lives
as long as the hub does.

### `roster` — query participants

Replies:

```jsonc
{"t":"roster","round":7,"max_rounds":24,"frozen":false,"muted":false,
 "seq":3,"goal":"find the regression",
 "peers":[{"name":"alpha","kind":"opencode","role":"Backend","state":"idle",
           "idle_for":72.0,"busy_for":0.0,"finished":false,
           "queued":2,"queued_from":["beta","frank"],"dropped":0,
           "context":4,"blocked_detail":null}]}
```

- `peers`: **every** participant, the querying client included — unlike
  `welcome.peers`, which lists only the others.
- `max_rounds`: the current round budget, so a client can render `round/limit`.
- `seq`: the query's `seq` echoed back, `null` when the query carried none —
  a client that polls in the background can tell its own replies apart.
- `goal`: present only while a goal is set.
- `idle_for`: seconds since the last frame from that participant.
- `busy_for`: seconds since it last became non-idle, `0.0` while it is idle.
- `queued`: wake messages waiting for it; `queued_from`: the senders of those,
  sorted and deduplicated.
- `dropped`: wake messages lost to queue overflow since the last flush.
- `context`: overheard messages currently buffered for it.
- `blocked_detail`: the `detail` of its current `blocked` report, else `null`.

### `bye` / `pong`

`bye` disconnects gracefully (the registration stays reclaimable). `pong`
answers a hub `ping`; any inbound frame counts as a life sign.

## Frames: hub → client

### `welcome`, `ok`, `err`

```jsonc
{"t":"welcome","name":"alpha","round":0,
 "peers":[{"name":"beta","kind":"opencode","role":"Tests","state":"idle"}],
 "limits":{"rate":"6/60s","max_rounds":24},
 "goal":"find the regression"}

{"t":"err","code":"rate_limited","retry_after":8.2,"seq":17,"detail":"..."}
```

`welcome.goal` is present only while a goal is set.

### `deliver` — coalesced delivery

```jsonc
{"t":"deliver","round":7,"msgs":[
  {"id":41,"from":"frank","to":"beta","addressing":"overheard",
   "kind":"question","text":"...","ts":1755859200.12,"seq":9},
  {"id":42,"from":"beta","to":"frank","addressing":"direct",
   "kind":"answer","text":"...","ts":1755859203.44,"seq":3}]}
```

- Always a list, ordered by message id, the result of coalescing. Message ids
  are unique across hub restarts within a day, and across a restart that
  crosses midnight: the counter is reseeded from yesterday's and today's
  transcript files at startup. Two exceptions: the three hub-generated context
  lines — `N older messages omitted`, `N addressed messages dropped while you
  were busy` and the `moot goal:` line — carry `id: 0` (the placeholders also
  `ts: 0.0`, and both placeholders can appear in one frame) and are not
  citable; and the mute/unmute system notes are wakes with real ids that are
  never written as `msg` records (they do appear in a `deliver` record's
  `msg_ids`), so a restart can hand their id to a later real message.
- `addressing` from the *recipient's* point of view: `direct` (own name),
  `broadcast` (`"*"`), or `overheard` (addressed to someone else).
- `to` is the *original* addressee, so overheard traffic stays attributable.
- `private` is present, and `true`, only on a private message — on the
  addressee's copy and on every observer's copy alike.
- A `deliver` to an **agent** always contains at least one `direct` or
  `broadcast` message; overheard-only frames are never sent to agents.
  **Observers are exempt**: they receive everything immediately, including
  overheard-only frames and private messages (`addressing` is pure rendering
  information for them).

### `event` — observer-only notifications

```jsonc
{"t":"event","event":"peer_joined","name":"gamma","kind":"opencode"}
```

Events: `peer_joined`, `peer_left`, `stall`, `round_limit`, `frozen`,
`resumed`, `reset`, `muted`, `unmuted`, `session_done`, `blocked`, `goal_set`,
`rejected`, `quorum`. Sent to observers only; extra fields vary
(`stall`/`quorum`/`blocked` carry `detail` — `blocked` also the agent's
`name` —, `round_limit` carries `round`/`max_rounds`, `peer_joined` carries
`role` in addition to `name`/`kind`, `goal_set` carries `text`,
`session_done` carries `round` — the wake counter the session closed at,
before the reset to 0). `peer_joined` is not sent to the joining participant
itself — a joiner learns the roster from `welcome.peers`.

`rejected` reports a `say` the hub refused at one of its floor checks (a
`malformed` say is answered with `err` but produces no event): `name` (the
sender), `code` (the
error code it got), `to`, `kind`, and `bytes` (the UTF-8 size of the text),
plus `retry_after` when the code is `rate_limited` and `private: true` when the
refused say was private. It never carries the text, and an observer's own
rejected `say` produces no event.

### `ping`

Sent after 300 s without traffic on a connection. The client must answer
`pong` within 30 s or the hub closes the connection (the registration stays
reclaimable). Any inbound frame resets the silence timer.

## Floor control (delivery semantics)

For each `say(from, to, …)`, every *other* connected participant is considered:

| recipient is… | addressing | delivery |
|---|---|---|
| observer | any | immediately, never queued, never buffered |
| agent, `to == recipient` | `direct` | **wake**: immediate if idle, queued while busy or blocked |
| agent, `to == "*"` | `broadcast` | **wake**: immediate if idle, queued while busy or blocked |
| agent, other addressee | `overheard` | appended to the context buffer; rides along on the next wake delivery |
| agent, other addressee, message is `private` | — | nothing: not buffered, not delivered, not in any later backlog |

A busy recipient's queue is flushed as **one** coalesced `deliver` when they
report `idle` (or are inferred idle): buffered overheard context and queued
wake messages, merged into one list ordered by message id. Caps: context
buffer 15 messages (FIFO eviction, a placeholder line reports the dropped
count), wake queue 100 messages (drop-oldest, placeholder line), at most 10
queued wake messages per `deliver` (the rest stays queued). While wakes remain
queued, only context lines older than the last wake in the slice ride along;
the slice that empties the queue drains the rest.

## Rounds, freeze, rate limit, dedup

- The round counter increments **once per wake occasion**: +1 per `say` that
  wakes at least one agent (regardless of how many), +1 per queue flush on an
  idle transition. Observer deliveries and system messages never count.
- At `max_rounds` (default 24, set per hub with `moot serve --max-rounds N`):
  `round_limit` event + notification, and the hub freezes — agent `say` is
  rejected with `err: frozen` (observers can still speak). `resume`/`reset`
  thaw. If no observer is connected when the hub freezes, that is logged as a
  warning: nobody can thaw it.
- Rate limit (agents only): token bucket, 6 messages / 60 s, burst 3.
  Rejection carries `retry_after` seconds.
- Dedup (agents only): an identical `(to, private, text)` from the same sender
  within 120 s is rejected with `err: duplicate`.
- Mute (opt-in, `/mute`): agent→agent says — including broadcast and private
  ones — are rejected with `err: muted`. Agent↔observer traffic flows.
- Checks run in this order: frozen, muted, recipient, rate limit, dedup — a
  say rejected before the rate check costs no token, and no rejected say
  enters the dedup window (a say rejected as `duplicate` has already spent
  its token).

## State inference (participants without `idle-events`)

- After a `deliver`: `busy` (this rule applies to every agent; the idle rules
  below only to participants without the capability).
- When any well-formed `say` arrives — accepted or rejected by a hub check:
  `idle` (they produced output — their queue then flushes). A `say` rejected
  as `malformed` never reaches these rules.
- 90 s after the delivery (or `state` report) that made them non-idle
  without a `say` in between: `idle`, with a `stall` event hint to observers.
  Pongs and other frames do not restart this timer — they prove the spoke
  process is alive, not that the agent produced output.
- A reported `blocked` is left by the same two rules (their `say`, or 90 s
  silence).

The inference is deliberately optimistic: better to classify idle too early
(batching then happens at the agent runtime) than to pin a session as busy.

## System messages

Agent-directed control information (mute/unmute transitions) is delivered as
a normal `deliver` from the reserved sender `system` with `addressing: "direct"`
and `kind: "note"` — it wakes idle agents and queues for busy ones, and is not
round-counted. The same reserved sender carries the hub's two placeholder lines
inside ordinary deliveries: the queue-drop notice (`addressing: "direct"`) and
the context-eviction notice (`addressing: "overheard"`), all three — the two
placeholders and the goal line below — with `id: 0` because they are not citable
messages. Clients must treat `from: "system"` as hub control plane, not as a
participant.

The session goal is the one `system` message that is *not* a wake: it is
appended to every agent's context buffer with `addressing: "overheard"` and
rides along on the next delivery.

## Watchdog behavior clients observe

Tick 5 s. `stall` (all agents idle, all queues empty, no accepted say, no agent
delivery, no roster change and no observer command for 60 s — fires once
until activity resumes; its `detail` reads `all agents idle, nothing queued —
unread context: <name N, …>; done: <names>; not done: <names>` — `unread
context` lists agents holding buffered lines nobody was woken for — an
exchange between others they only overheard, or the `/goal` line; a private
say is never context, it wakes its addressee or waits in its queue (the gate
deliberately ignores buffered context), each list sorted and `—` when empty;
after a `session_done` and until the next accepted agent say the last clause
reads `session done at HH:MM:SS` instead of the done/not-done split),
`quorum` (fires once per observer say that addresses agents — `*` asks every
agent connected at that moment, `@name` that agent — at the first tick after
every asked agent has had a say accepted strictly later than the observer's,
all agents are idle and every queue is empty; agents that left are not waited for; `detail` reads `every addressed
agent has answered — <names>`; re-armed only by the next such observer say),
`blocked` (an agent reports `blocked` for > 60 s, with its `detail`; it fires
once per blocked episode — a repeated `blocked` with the same `detail` does not
restart the timer), and the ping/pong dead-connection detection above. The
watchdog's stall, round_limit, blocked, and session_done additionally raise
macOS notifications (unless the hub runs with `--no-notify`), `quorum` too
(subject to the presence rule below); the assumed-idle `stall` hint from state
inference does not. Routing raises two more: an agent's direct `say` to an
observer, and an agent's broadcast of kind `question`. Both carry
`<sender> → <to> · <kind>: <text>` with the text collapsed to one line and cut
to 80 characters. **Presence rule:** an observer counts as present while its
last inbound frame is younger than 60 s (`Config.present_within`; the TUI's 3 s
roster poll keeps an open seat present, a classic seat is present only right
after the human typed — or for 60 s after it answered a hub `ping`). The direct one fires only when the addressed observer is
absent; the broadcast one and `quorum` fire only when no connected observer is
present — so a question aimed at a human with no seat open still reaches them,
and a human at the seat is not paged for what is on screen. Stall, blocked,
round_limit and session_done are never suppressed.

## Error codes

| code | meaning | extra fields |
|---|---|---|
| `proto_mismatch` | `hello` with unknown `proto` version | — |
| `name_taken` | name held by a live connection | `detail`: free suggestion |
| `unknown_recipient` | `to` is not connected | `detail` includes the current roster |
| `rate_limited` | token bucket empty | `retry_after` (seconds) |
| `duplicate` | identical `(to, private, text)` in the dedup window | — |
| `frozen` | hub frozen (round limit or manual `freeze`), waiting for `resume`/`reset` | — |
| `muted` | mute mode blocks agent→agent | — |
| `frame_too_large` | > 256 KiB; connection closes after drain | — |
| `malformed` | invalid JSON/UTF-8, missing field, unknown frame type, invalid enum value | — |
| `forbidden` | `cmd` from a non-observer | — |

`malformed` replies echo `seq` when the offending frame carried an integer
`seq` — except the two pre-dispatch cases (`hello expected first`, and a
second `hello` on a registered connection), which answer `seq: null`.
