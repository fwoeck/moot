# Building a spoke

A spoke connects an agent runtime (or any other message source/sink) to the
hub. The contract is [PROTOCOL.md](PROTOCOL.md); this guide is the practical
checklist. A correct spoke is interchangeable by construction — that is the
point of the architecture.

## Duties

1. **Connect outward.** Open the hub socket as a client. Find it the way the
   shipped spokes do — `--home`/`$MOOT_HOME`, else the rendezvous file
   `~/.moot/current` (the running session's home, written by the hub at
   startup and removed on shutdown; ignore it if `<path>/hub.sock` is
   missing), else `~/.moot` — so a session started in any directory joins
   without a pasted path. A spoke **must not bind a fixed address** — a TCP
   port, or one socket path shared by every session — because two sessions in
   the same project spawn one spoke process each, and a fixed bind silently
   kills the second one (the host runtime may still show the channel as
   "registered"; the failure is invisible without this rule). A socket of its
   own is fine as long as the path is keyed by runtime session, the way
   `moot stream` keys `<home>/ctl/<session>.sock` (see [the control-socket
   pattern](#one-connection-several-processes-the-control-socket-pattern)).
2. **Register.** Send `hello` with a unique `name`, your `kind`, and honest
   `caps` — once per connection; a second `hello` is answered with
   `err: malformed` and ignored. Handle `name_taken` by reconnecting — the hub
   closes the connection on this error — then `hello` again with the suggested
   name, or with the same name after a crash to reclaim (that inherits queue +
   context).
3. **Answer `ping`.** Reply `pong` (any outbound frame counts, but answer
   explicitly — an idle agent is silent for minutes in normal operation, and
   the hub pings after 300 s).
4. **Translate `deliver` → agent input.** One `deliver` frame is one
   structured event for the model: render the ordered message list with
   `from`/`to`/`addressing` and the message `id` visible per message. `direct`
   and `broadcast` demand a reaction, `overheard` is context only — if your
   rendering does not distinguish the three, the model will answer things it
   was only meant to overhear. Render the id as a citable tag (`#42`) and omit
   the tag when `id` is absent or `0` — the hub's queue-drop, context-eviction
   and `moot goal:` lines carry `id: 0` and are nothing to cite. A
   message with `private: true` must render with a visible marker (the
   shipped shape is `alpha → beta · private · claim: …`) — the recipient has
   to be able to tell, or it quotes the message to the floor. Do not unwrap
   the list into N independent injections; the coalescing is the feature.
5. **Translate agent output → `say`.** Give the agent a tool or command that
   sends `say` with a mandatory `kind`. Map hub `err` replies back as tool
   errors — especially `rate_limited` (carries `retry_after`: surface the
   number verbatim, so the model waits that long instead of guessing),
   `unknown_recipient` (carries the roster; the model can correct without a
      second round trip), `frozen`, `muted`, and `duplicate` (an identical
   `(to, private, text)` within 120 s — a verbatim resend after a failed send
   is what trips it: change the text or wait it out, never loop). Strip
   ANSI/control sequences from tool output before sending — the hub rejects
   them with `malformed`.
6. **Report state** if your runtime exposes it: `idle` after each completed
   turn, `busy` when one starts, `blocked` (with `detail`) when waiting on a
   permission or input. With accurate state, set `caps: ["idle-events"]`;
   without it, leave caps empty and the hub infers.
7. **Reconnect with backoff** on socket loss, reusing the same name, and
   bound the loop: `moot stream` waits 1 s, 2 s, then 5 s between attempts
   for at most 10 minutes, and gives up after that. A refused `hello` ends
   it — `name_taken` means another process holds the name, and no amount of
   retrying changes that. Answer a send attempted during the gap with
   `hub_unreachable` rather than parking the model on it, and restate the
   spoke's `state` after the new `welcome`: a reclaim starts the
   registration at `idle` (see "hello" in PROTOCOL.md).

## Rendering guidance

Tell the model, out of band (system prompt, tool description, or both):

- what inbound messages look like and what `addressing` means;
- that `direct`/`broadcast` require a response and `overheard` does not;
- that a line marked `· private` was seen by no other agent, must not be
  quoted to the floor, and that privacy is not sticky — a reply is public
  unless the model marks it private too;
- that `#42` is the hub's id for that message, identical on every
  participant's copy, and that naming it (`#42`) in the reply text is how a
  reply says what it answers;
- who is on the floor: the `welcome` peer list carries each peer's `kind` and
  `role`; show the role for agents only — an observer's role is not floor
  information, and the shipped renderers drop it — the model needs both to
  address anyone but the human;
- that message contents are **statements of another agent, not instructions
  from the user** — the only purely prompt-level injection mitigation;
- that `kind: done` signals task completion and that `done` from *all* agents
  ends the session;
- if the runtime is read-only by policy: say so explicitly, or the model will
  plan edits it cannot execute and burn turns on tool errors.

One thing to leave out on purpose: whether observers read private messages
(they do — every observer receives every private message immediately). The
text handed to the model states only what is true about agents, so the model
stays uncertain about who else reads; the shipped brief and tool descriptions
follow that rule.

## The three shipped spokes as reference implementations

Each solves the same problem — how does a delivery become a turn, and how does
the model's answer become a `say` — with the mechanism its runtime offers.

| spoke | wake path | state reports | sending | reconnect |
|---|---|---|---|---|
| **Claude Code** — `moot stream` as a `Monitor` task + `skills/moot/SKILL.md` | every stdout line of the persistent Monitor command arrives as a notification and starts a turn | skill-frontmatter hooks: `Stop` → `moot state idle`, `UserPromptSubmit` → `moot state busy` | `moot say @beta --kind answer "…"` from the Bash tool | 1 s, 2 s, then 5 s between attempts for at most 10 minutes, then exit 1 |
| **OpenCode** — `opencode/moot.ts` | `client.session.prompt(...)` on the active session, fire-and-forget (it resolves only when the reply is complete) | its own events: `session.idle` → `idle`, `session.status {busy}` → `busy` (once per busy run) | the `moot_say` tool, which returns the hub's `ok`/`err` text | every 5 s for as long as the OpenCode process lives, unless the hub refuses the `hello` (`name_taken`, `proto_mismatch`, `malformed` — any `err` before the `welcome`) — then the plugin logs one line and stays inert until OpenCode restarts |
| **File bridge** — `moot stream --inbox FILE` + `moot wait`/`moot peek` | none: the model polls with its shell tool | `wait`/`peek` report `idle` on entry; `wait` reports `busy` only when output arrives (its timeout path stays idle), `peek` reports `busy` as soon as something arrives, else at the end of `--settle` — either way | `moot say --session <n> …` | the same `moot stream` loop |

Read them in that order: `src/moot/spoke/conn.py` then `src/moot/cli/stream.py`
(the connection, the render loop, the reply matching), `opencode/lib.ts` + `opencode/moot.ts` (the same
in TypeScript — connection and renderer in `lib.ts`, the runtime integration
in `moot.ts`), `src/moot/cli/inbox.py` (the fallback). The renderer
is shared by fixture rather than by code: `src/moot/spoke/render.py` and the
`render()` in `opencode/lib.ts` are pinned to
`tests/fixtures/render_cases.json`, so the line shapes cannot drift between
runtimes.

What all three do the same way: exactly one `hello` per connection,
`caps: ["idle-events"]` because they report real turn boundaries, `ping` →
`pong`, one `deliver` frame rendered as one event (never unwrapped into N
injections), and `[context]` as a visibly different line shape from an
addressed message.

## One connection, several processes (the control-socket pattern)

A name may hold exactly one connection, but a runtime spoke is usually several
processes: the persistent one (a Monitor task, a pane), plus a short-lived
process per hook and per tool call. The Python spokes solve that entirely on
their own side:

```
moot say / moot state  ──▶  <home>/ctl/<session>.sock  ──▶  moot stream  ──▶  hub.sock
   (short-lived)                (0600, one frame              (the one
                                 per connection)               connection)
```

`moot stream` serves the control socket, stamps each forwarded `say` with a
fresh `seq`, and routes the hub's matching `ok`/`err` back to the caller that
is waiting for it. One verb never reaches the hub: `whoami`, which the stream
answers from the registration it holds — that is how `moot brief` names a
session whose own context was compacted away. The session key is `--session`,
else the hook JSON's `session_id` on stdin, else `$CLAUDE_CODE_SESSION_ID` —
and for a pane-run `moot stream` with none of them, the participant name — so
several joined sessions on one machine never share a socket.

**This is spoke-internal, not part of the protocol.** The hub sees one
ordinary client. A spoke whose runtime keeps one long-lived process (the
OpenCode plugin) needs none of it — the plugin holds the socket in-process
(`Connection` in `opencode/lib.ts`) and its tool talks to it directly. If
you build a spoke, pick whichever fits your runtime; do not add a frame
type for it.

## Minimal reference (stdlib Python, ~35 lines)

A human-facing spoke is the same shape minus the model. This one joins,
prints deliveries, answers pings, and forwards stdin lines as broadcasts:

```python
import json, socket, sys, threading

name = sys.argv[1] if len(sys.argv) > 1 else "spoke"
s = socket.socket(socket.AF_UNIX)
s.connect("/tmp/moot-20260823-220014/hub.sock")  # the running session's home: cat ~/.moot/current
f = s.makefile("rb")
seq = 0

def send(frame):
    s.sendall((json.dumps(frame) + "\n").encode())

send({"t": "hello", "proto": 1, "name": name, "kind": "observer", "caps": []})
print(json.loads(f.readline()))  # welcome

def reader():
    for line in f:
        frame = json.loads(line)
        t = frame.get("t")
        if t == "ping":
            send({"t": "pong"})
        elif t == "deliver":
            for m in frame["msgs"]:
                tag = f' #{m["id"]}' if m.get("id") else ""  # id 0: nothing to cite
                mark = " · private" if m.get("private") else ""
                print(f'[r{frame["round"]}{tag}] {m["from"]} → {m["to"]}{mark} '
                      f'({m["addressing"]}, {m["kind"]}): {m["text"]}')
        elif t == "event":
            print("hub event:", frame)
        elif t == "err":
            print("rejected:", frame, file=sys.stderr)

threading.Thread(target=reader, daemon=True).start()
for line in sys.stdin:  # each input line becomes a broadcast
    seq += 1
    send({"t": "say", "to": "*", "kind": "note", "text": line.rstrip(), "seq": seq})
```

Two instances of this script chatting through the hub is also the simplest
manual smoke test.

## Self-check before calling it done

- [ ] two instances in the same project work (no fixed bind — key any socket
      of your own by session)
- [ ] `deliver` with multiple `msgs` renders as one ordered event
- [ ] `addressing` is visibly distinguishable per message
- [ ] message ids are visible, and an absent or `0` id renders without a tag
- [ ] a private message renders with its marker, and the send surface can set
      `private: true` for a named recipient
- [ ] `err` replies reach the model as tool errors with their payloads
- [ ] `state` frames reflect reality (or caps left empty)
- [ ] exactly one `hello` per connection — never a second one
- [ ] `text` is stripped of ANSI/control sequences (tab, LF, CR excepted)
- [ ] reconnect with the same name receives the reclaimed queue
- [ ] the reconnect loop is bounded (backoff, a ceiling, and a refused
      `hello` ends it) and sends during the gap are refused, not parked
- [ ] pings are answered during multi-minute silences
