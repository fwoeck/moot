# Manual test: two runtime sessions and a human on the floor

A reproducible end-to-end run: the hub, your observer seat, a **Claude Code**
session that joins through the skill, and an **OpenCode** session that joins
through the plugin. Nothing is pasted into the models by hand — each spoke
brings its own briefing, wakes its session on delivery, and reports real turn
boundaries.

What this exercises: floor control, coalescing, rounds and freeze, mute,
reclaim, stall detection, and both push paths (Claude Code `Monitor`,
OpenCode `session.prompt`).

---

## 1. Prerequisites and install

- Python ≥ 3.13, [uv](https://docs.astral.sh/uv/), `tmux`, `jq`.
- Claude Code with skill-frontmatter hooks and the `Monitor` tool (verified on
  2.1.241) and/or OpenCode with plugin support (verified on 1.18.21).
- The repository cloned and synced:

  ```bash
  git clone <repo-url> ~/development/moot && cd ~/development/moot && uv sync
  ```

### `moot` on PATH

Hooks and the OpenCode plugin run outside your interactive shell, so the
command has to be findable without a `uv run` prefix:

```bash
uv tool install --editable ~/development/moot   # into `uv tool dir --bin` (~/.local/bin)
moot doctor                                     # check: the install rows say pass
```

Without the tool install, prefix every command with the venv path
(`~/development/moot/.venv/bin/moot`) — including the ones in the skill and in
the fallback instructions. The hub itself needs neither: `uv run moot serve`
works from the repo.

### Claude Code: the skill

```bash
mkdir -p ~/.claude/skills
ln -s ~/development/moot/skills/moot ~/.claude/skills/moot     # all projects
# or, for a single project:
ln -s ~/development/moot/skills/moot <project>/.claude/skills/moot
```

The symlink is followed; the skill stays editable in the repo. One-time, in
`~/.claude/settings.json` (or the project's `.claude/settings.json`), so the
model's `moot` calls are not prompted for on every turn:

```jsonc
{"permissions": {"allow": ["Bash(moot *)"]}}
```

Invoking `/moot join …` registers three hooks for the rest of that session:
`Stop` → `moot state idle`, `UserPromptSubmit` → `moot state busy`,
`SessionStart(compact)` → `moot brief --runtime claude-code` (the rules are
re-injected after a compaction, which drops the skill body). That re-brief
carries no `--name`, so it asks the session's `moot stream` who it is and
comes back with the name and role — or, if the stream is gone, with a line
telling the model to have you re-join.

### OpenCode: the plugin

```bash
mkdir -p ~/.config/opencode/plugins
ln -s ~/development/moot/opencode/moot.ts ~/.config/opencode/plugins/moot.ts
# or project-scoped: <project>/.opencode/plugins/moot.ts

cd ~/development/moot/opencode && bun install   # one-time: see below
```

The plugin imports `@opencode-ai/plugin`, and OpenCode resolves it from the
symlink's *target* — so without `opencode/node_modules` the plugin's import
fails and OpenCode skips it **silently** (the error only reaches the TUI's
error event, not `~/.local/share/opencode/log/opencode.log`). `bun install`
in `opencode/` makes the symlinked plugin loadable; without the symlink
(real copies of `moot.ts` + `lib.ts` in the plugins dir) the package resolves
from `~/.config/opencode/node_modules` instead and this step is not needed.

The plugin needs its name at launch:

```bash
MOOT_NAME=beta MOOT_ROLE="verification" opencode
```

Without `MOOT_NAME` it logs one line and stays inert — that is how you keep
OpenCode usable for unrelated work. `MOOT_HOME` overrides the state directory;
normally the rendezvous file does that job.

### Check the pieces before you start

```bash
moot doctor                                     # install, symlinks, bun, orphans
moot brief --runtime claude-code --name alpha --role diagnosis   # the skill's rules
moot brief --runtime opencode --name beta --role verification # the plugin's rules
bun test opencode/                              # optional: the plugin's own tests
```

`moot doctor` prints one `pass`/`FAIL` row per check and always exits 0; a
failing row carries the command that fixes it. The `hub`, `ctl sockets` and
`~/.moot/current` rows are about a *running* session: before you start,
`ctl sockets` and `~/.moot/current` pass with nothing to report, while `hub`
reads `FAIL … no hub at …` — expected here, and its `moot serve --home` hint
is not what you want: start the session with `scripts/session.sh` instead.

---

## 2. The session

```bash
cd ~/development/moot && scripts/session.sh --no-notify
```

One tmux window, two panes: the hub on top, your observer seat below (that is
where you type). The script prints the state directory, the transcript
directory and the join instructions — plus, when you ran it outside tmux, the
line to attach with:

```
moot session started in tmux window 'moot'  (home: /tmp/moot-20260825-104233)
  top: hub   bottom: observer (type here)
    seat: frank  (the name agents address you by; the band shows it as @frank)
  transcripts: /Users/frank/.moot/sessions/20260825-104233/transcripts  (also reachable as /tmp/moot-20260825-104233/transcripts)

The hub wrote ~/.moot/current, so the spokes below find this session without
a path. They need `moot` on PATH: `uv tool install --editable ~/development/moot`
(or prefix every command with ~/development/moot/.venv/bin/).

Claude Code: in your project run  /moot join alpha "<role>"
OpenCode:    start it with        MOOT_NAME=beta MOOT_ROLE="<role>" opencode
Fallback:    pane: moot stream --name <n> --session <n> --inbox <home>/<n>.in
             model: moot wait --session <n> --inbox <home>/<n>.in  /
                    moot peek --session <n> --inbox <home>/<n>.in  /
                    moot say --session <n> @alpha --kind answer "…"

attach with:  tmux attach -t moot
```

**1. Claude Code.** In the project you want diagnosed:

```
/moot join alpha "diagnosis"
```

The skill starts one `Monitor` task running
`moot stream --name alpha --kind claude-code --role "diagnosis"`. Its first
line is the join confirmation:

```
[moot] joined as alpha (claude-code) · peers: frank (observer, idle) · round 0/24
```

**2. OpenCode.** In the same (or another) project:

```bash
MOOT_NAME=beta MOOT_ROLE="verification" opencode
```

The plugin connects at startup. It logs its join line through OpenCode's own
logging, not into the TUI — the reliable confirmation is in your observer
pane: `· peer_joined name=beta kind=opencode role=verification`.

**3. Your seat.** The observer pane is a small TUI: a scrolling message area
above a sticky input footer. It shows the welcome and both joins in the
message area while you type undisturbed below:

```
22:51:44 welcome frank  round 0  peers   limits 6/60s, 24 rounds
22:51:45 · peer_joined name=alpha kind=claude-code role=diagnosis
22:51:45 · peer_joined name=beta kind=opencode role=verification
───────────────────────────────────────enter: send · alt-enter: newline─
› 
```

The divider row carries the floor's live picture once the first roster poll
(every 3 s) answers — your own floor name first, then the round, every agent
with its state, queue and unread context, and the goal when one is set:

```
──@frank · r4/24 · alpha busy 1m12s 2q(frank,beta) 2c · beta idle · goal: find the leak─
```

Type to send. Plain text is a broadcast, `@beta text` is direct, `!kind text`
sets the kind (`claim`, `question`, `answer`, `result`, `objection`, `done`,
`note`; default `note`), and `@beta !private text` is a direct message only
beta and the observers receive; the prefixes work in any order. The command
table in [README](../README.md) → Your seat lists every command.

Editing in the footer: arrows/Home/End, Ctrl-A/E/K/U/W, Alt-B/F for words,
Up/Down for the global history (`~/.moot/observer_history`, shared across
sessions), Alt-Enter for a newline inside the message (a pasted multi-line
text likewise becomes one message), Ctrl-C clears the buffer (on an empty
buffer it quits), Ctrl-D quits on an empty buffer (and deletes the character
at the cursor otherwise), Ctrl-L clears the message area and redraws the
footer (the messages stay in scrollback). Control characters in a paste are
displayed in caret notation (`^[` and the like); the seat refuses the line
before it is sent, naming the column, and keeps your buffer — as it does for
an empty message, an unknown peer (`[no peer 'bta' — alpha, beta]`) and an
unknown command.

Your own sends echo in the message area when the hub answers `ok`, with the
id it assigned:

```
22:51:48 frank → beta note  the index is missing  #47
```

A rejected send appears only as a red error line. Scrolled-off lines are
ordinary terminal scrollback: scroll with tmux as usual. `--no-tui` (or any
non-terminal stdin/stdout) gives the plain line mode: same input syntax, the
same commands and the same rendered lines, but without the footer, the status
band, the editing/history, and the echo of your own sends.

**4. Run it.** Broadcast the task:

```
alpha: propose the root cause of <bug>. beta: try to refute it.
```

Both sessions are idle, so both are woken at once: Claude Code starts a turn
from the Monitor notification, OpenCode from the injected prompt (which
carries the operating rules the first time).

---

## 3. What to expect on the floor

**Delivery timing.** The hub delivers to an idle participant immediately and
queues for a busy one.

| recipient | idle | busy (mid-turn) |
|---|---|---|
| Claude Code | the Monitor line arrives as a notification and starts a turn | queued in the hub until the `Stop` hook reports `idle`, then delivered coalesced |
| OpenCode | `session.prompt` starts a turn right away | queued until `session.idle`; OpenCode itself also picks a prompt up only at its next turn boundary |
| fallback (`--inbox`) | the line is in the file; `moot wait` returns with it | queued until `wait`/`peek` reports `idle` |

Nothing here is a 90 s guess: both spokes declare `caps: ["idle-events"]` and
report real turn boundaries, so the hub knows the state instead of inferring
it.

**What a session sees.** One rendered line per message, text verbatim:

```
[r3 #58] frank → alpha · question: what evidence would change your mind?
[context #57] beta → frank · answer: the trace shows the second call, not the first
[r3 #59] beta → alpha · private · claim: the second call is yours, not mine
```

`#58` is the hub's message id, the same number on every participant's copy —
the handle a reply cites. `· private` marks a message only its addressee and
the observers received (`moot say --private`, `moot_say` with
`private: true`); no other agent session ever sees that line, not even as
`[context]`. `[context]` lines are traffic between *other*
participants that rides along on the next wake — background, never something
to answer. That is floor control: everyone sees everything that is not
private, but only addressed messages start a turn.

That is the whole vocabulary a session sees. `[event]` lines are
observer-only, so a frozen floor reaches the model the other way round: its
next `moot say` fails with `err frozen: hub is frozen — waiting for resume`,
which is what `moot brief` tells it to expect. Send errors in general —
`frozen`, `muted`, `rate_limited`, `unknown_recipient` — are the exit-1 output
of `moot say` (the `moot_say` tool result in OpenCode), not stream lines: they
carry the `seq` of the send and are handed back to the caller that is waiting
for it. An `[err]` line appears in the stream only for an err with no waiting
send to match it.

**What you see.** One line per statement, newest at the bottom:

```
22:51:47 alpha → beta claim  the bug is in X  r1 #42
22:51:47 beta → * objection  no, X is fine  r2 #43
```

`r1` is the round, `#42` the hub's message id — the handle `/show #42` and
`/q 42 @alpha …` take (`/last [n]` takes a count, not an id).

`→ you` is bold, events are dimmed, errors red, long texts cut to the terminal
width (`--full` / `--width N` in `moot observe`; the full text is always in
the transcript). A multi-line message shows as one line — newlines collapse
to spaces; `--full` renders every line, aligned under the text.

**State transitions.** The divider band shows the picture continuously —
it starts with your own floor name (`@frank`), and an agent holding lines
nobody woke it for shows `Nc` after its state; `/roster` prints it as a line
— round, freeze/mute flags, every peer's state, and the goal when one is set:

```
22:51:47 roster r2  frank:idle, alpha:busy, beta:busy  goal: find the leak
```

A participant goes `busy` when a delivery reaches it (and when its runtime
says so) and `idle` when its turn ends — the Claude Code `Stop` hook, the
OpenCode `session.idle` event, or a `wait`/`peek` call in the fallback. `moot
stream` never invents a state of its own.

**The transcript** is the history that survives compaction:

```bash
moot log --home <home>                          # today's messages, one per line
moot log --home <home> --kind all --since 14:00 # events and deliveries interleaved, from 14:00
moot log --home <home> --format md --out session.md
```

`moot log` reads today's file only. For an earlier day, or for the
`session`/`session_end` records:

```bash
jq -r 'select(.type=="msg") | "r\(.round) \(.from) → \(.to) [\(.kind)]: \(.text)"' \
  <home>/transcripts/*.jsonl
```

`type` is `msg`, `deliver`, `event`, or the `session`/`session_end` pair that
brackets the hub run; `moot log` renders `msg`, `event` and `deliver` records
— only the `session`/`session_end` pair is never a line. `state` frames are
*not* transcribed — use `/roster` for the live picture and
`moot log --kind deliver` for the who-got-what history.

---

## 4. Things worth breaking on purpose

- **A private exchange** — from your seat, `@alpha !private psst`. Your echo
  line carries `· private`, alpha's session shows
  `[r1 #N] frank → alpha · private · note: psst`, and beta's next wake carries
  no trace of it — no `[context]` line, nothing in a later backlog. From a
  session: `moot say --private @beta …` (Claude Code) or `moot_say` with
  `private: true` (OpenCode). A private send to `*` is refused.
- **Quorum** — address both sessions (`hello both`) and wait for both
  answers: within one tick of the second `idle` the pane shows
  `quorum detail=every addressed agent has answered — alpha, beta`. Once per
  message from you.
- **Stall** — stop talking. Once every session is idle and 60 s pass without a
  say or delivery, your pane shows `stall detail=all agents idle, nothing
  queued — unread context: —; done: beta; not done: alpha` (~65 s with the
  5 s tick). A macOS notification fires with it unless the hub was started
  `--no-notify`, as the `session.sh` line in §2 does. A session that is
  working is busy, so no stall fires while an agent is composing — however
  long that takes.
- **Round limit** — 24 wake occasions freeze the floor: agents get
  `err frozen: …` on their next send and are told by their briefing to wait
  for you; `/resume` grants a fresh budget, `/resume 12` adds 12 rounds. To
  reach the limit quickly, start the session with
  `scripts/session.sh --max-rounds 2`.
- **Rate limit** — 6 messages per 60 s, burst 3. A model that sends four
  messages in a row gets `err rate_limited: rate limit · retry in 7.184s` on
  the fourth. Repeat one text and the second and third come back as
  `err duplicate: identical message within dedup window` (120 s) — the rate
  check runs first, so a duplicate still spends its token and the fourth is
  `rate_limited` all the same. The wait comes from the hub's `retry_after`
    and all three surfaces show it: `moot say`, the `moot_say` tool, and your
  observer pane, where it rides on the `rejected` event
  (`· rejected name=alpha code=rate_limited to=* kind=note bytes=NN
  retry_after=7.184`). The briefing tells the model to wait
  for its next turn — never to sleep, never to retry in a loop.
- **Mute** — `/mute`: the agents can then only talk to you, and receive a
  system note saying so. `/unmute` restores.
- **Dead spoke** — quit the Claude Code session (or kill its Monitor task)
  while a message is queued for it, then rejoin with the same name: the
  reclaim delivers the queued message. Messages sent *while* the name is gone
  are not held: a direct one is answered `err unknown_recipient` (its detail
  lists who is there), a broadcast goes to the others only. Only the
  broadcast reaches the transcript as a message: an agent's rejected direct
  send leaves a text-less `rejected` event, an observer's leaves nothing.
- **Compaction** — run `/compact` in the joined Claude Code session. Expected:
  the Monitor task keeps running (send a message from your seat afterwards to
  confirm the session still wakes) and the `SessionStart(compact)` hook
  re-prints the operating rules, which the compacted context does not hold.
  Compaction survival is the one part of the design no automated test covers,
  so this step is where it is verified. The re-brief reports the session's own
  name (`` You are `alpha` ``), so a floor that broke shows up right there as
  `your floor connection is gone — ask the user to run /moot join again`; then
  `/moot join alpha` again — the skill skips the Monitor only when a task with
  that description still exists.
- **Shutdown** — Ctrl-C in the hub pane with everything connected: the hub
  exits within a second, `hub.sock` and `~/.moot/current` are gone, and every
  `moot stream` prints `[moot] hub closed — retrying for up to 10 min` and
  stays up (the model and you both see it).
- **Restart under a live session** — after that Ctrl-C, start the hub again
  on the same home. Expected: within a few seconds each stream prints a
  `[moot] rejoined as …` line carrying the new round and peer list, the
  session goes on without rejoining by hand, and the state its hooks last
  reported is back on the hub (check `/roster` from the seat: a session that
  was busy is still busy). A `moot say` sent during the gap comes back as
  `err hub_unreachable: the hub is gone — the stream is retrying`. Leave the
  hub down for ten minutes instead and the stream prints
  `[moot] hub gone — giving up` and exits 1.

---

## 5. Troubleshooting

The three platform mechanisms this depends on can be verified in isolation.
Run these recipes when a spoke does not wake, before suspecting the hub.

**A Claude Code session never wakes on delivery.** Test the push path itself:

```jsonc
// in the session, invoke the Monitor tool:
{"command": "tail -n 0 -f /tmp/moot-spike.log", "description": "moot spike", "persistent": true}
```

End the turn, then from another terminal `echo hello >> /tmp/moot-spike.log`.
A new turn must start with that line. If it does not, the Monitor path is the
problem, not moot. If it does, check that the Monitor task in the session runs
`moot stream …` and did not exit (a stream whose hub stays away exits 1 after
ten minutes of retries, printing `[moot] hub gone — giving up`).

**An OpenCode session never wakes.** Drop a spike plugin next to the real one:

```ts
// ~/.config/opencode/plugins/moot-spike.ts
import { appendFileSync } from "node:fs"
export const Spike = async ({ client }) => ({
  event: async ({ event }) => {
    appendFileSync("/tmp/moot-spike-events.log", `${event.type} ${JSON.stringify(event.properties)}\n`)
    if (event.type === "session.idle") {
      const id = event.properties.sessionID
      setTimeout(() => {
        void client.session.prompt({ path: { id }, body: { parts: [{ type: "text", text: "SPIKE-OK" }] } })
      }, 5000)
    }
  },
})
```

Start OpenCode, let it go idle, wait 5 s: a turn must start with `SPIKE-OK`.
The log shows which events fire and in which order (observed on 1.18.21:
`session.created` → `message.updated` → `session.status {busy}` → … →
`session.status {idle}` → `session.idle`). Remove the spike plugin afterwards —
two plugins injecting prompts is confusing.

**Hooks do not fire, or `moot state` has no session id.** Register a throwaway
skill whose frontmatter has `Stop: [{hooks: [{type: command, command: "cat >
/tmp/moot-hook.json"}]}]`, invoke it, end a turn: the file must contain
`session_id`, `hook_event_name`, `cwd`. `moot state` takes the session id from
that JSON on stdin, from `--session`, or from `$CLAUDE_CODE_SESSION_ID`.

| Symptom | Cause / action |
|---|---|
| `moot: no moot stream for session <id>` on stderr | the hook found no control socket for this session: the stream never started, or has ended. Expected noise before `/moot join` and once the stream itself is gone — a hub that merely went away does not cause it, the stream keeps its control socket through the gap; a `Stop` hook exiting 1 does not block the session. If it persists *during* a session, the Monitor task died — rejoin. |
| `moot: command not found` in a hook or in the plugin | `moot` is not on the PATH those processes inherit — `uv tool install --editable .`, or use absolute paths. The plugin falls back to a two-line built-in notice when `moot brief` fails and logs why. |
| `HubError: name_taken: <suggestion>` at join | a live connection holds the name. Take the suggested alternative, or find the stale process. After a crash the same name reclaims its queue — no need to rename. |
| the plugin says `hello rejected: …` | the hub refused the handshake (name taken, protocol mismatch); the plugin stays inert for the rest of that OpenCode process. Restart OpenCode with a free `MOOT_NAME`. |
| `OSError: AF_UNIX path too long` from `moot stream` | macOS caps AF_UNIX paths at 104 bytes and the control socket is `<home>/ctl/<session>.sock` with a 36-char session id. Use a short home (`/tmp/moot-…`), which `session.sh` does. |
| the observer pane shows nothing after a send | check `/roster`: `busy` means the recipient's queue waits for an idle report; a `stall` event means everybody is idle and nobody was addressed. |
| hub froze and no observer is connected | only an observer can `resume`/`reset`. Start `moot observe --home <home>` and thaw. |

---

## 6. Caveats that remain

- **Mid-turn messages queue.** A message that arrives while a Claude Code
  session is working is held by the hub until the `Stop` hook fires — that is
  the design (it is why deliveries are coalesced), but it means a peer can
  wait a whole turn. A busy OpenCode session is the same case: even a prompt
  that is admitted is picked up only at the next turn boundary.
- **Turns take minutes**, so two agents can answer each other's *previous*
  message. The "one reply per delivery, no unsolicited follow-ups" rule in
  `moot brief` exists to break that pattern; the hub cannot, because queuing
  for a busy agent is exactly its job.
- **Hook noise around the edges.** `moot state` runs on every turn boundary
  for the rest of the Claude Code session, and once the session's
  `moot stream` has exited it prints `no moot stream for session …` to stderr
  on every one of them. A hub that merely went away does not cause this — the
  stream keeps its control socket through the gap and answers `moot state` —
  so the noise starts when the stream itself is gone. Harmless by design (exit
  1 never blocks a turn), but it appears in the session's hook output until
  you restart the session.
- **The fallback needs a reading command.** `moot stream --inbox FILE` only
  writes the file; the *model* must run `moot wait` (or `moot peek`) for its
  session to be reported idle. A session that reads the inbox with `cat`
  instead never reports idle, stays busy after its first delivery, and
  everything for it queues forever. `wait`/`peek` also need `--session`
  matching the stream's: they do fall back to `$CLAUDE_CODE_SESSION_ID`, but
  in the fallback runtime it is unset, and inside a Claude Code session it
  names that session — a different ctl socket than the pane's stream.
- **A rejoin restores your seat, not the floor.** After a hub restart each
  `moot stream` is back under the same name with its last reported state, but
  the new hub is a new floor: the round counter starts at 0, the goal is gone
  (`<home>/goal` is removed at shutdown — set it again with `/goal`), and wake
  messages that were queued when the old hub died were only ever in memory.
  What survives is the transcript, which the new hub reads for the join
  backlog.
- **One name, one connection.** Both spokes hold exactly one hub connection
  per runtime session; `moot say`/`moot state` reach it through the control
  socket. Never start a second `moot stream` for the same name.
- **A fresh OpenCode TUI delivers on first interaction.** The plugin adopts
  a seat only once OpenCode has a session (`session.created` fires with the
  first prompt), so a message that arrives before the human has typed
  anything is held by the plugin and injected right after the first turn
  ends. Type anything ("hi") after launching OpenCode and the floor flows;
  until then, held messages wait.
- **One seat per OpenCode process.** Deliveries go to the top-level session
  that most recently finished a turn (subagent task sessions never take the
  seat). That assumes the single user this is built for.
- **`ok` confirmations are not shown** to the model beyond
  `ok → beta · #47` — the hub's id for that message, which a peer can cite
  back as `#47`; errors are. An agent that sends and then waits sees nothing
  until a peer replies — that is what `--timeout` is for in the fallback.
- **Starting the hub in the background** from a non-interactive shell makes
  the child inherit SIGINT as ignored; stop it with SIGTERM (both run the same
  teardown). In a terminal, Ctrl-C works as documented.
- **The inbox cursor** lives in `<inbox>.cursor`; delete it to re-read from
  the start. To forget a session entirely, stop the hub and delete both its
  state directory and its transcript directory — with `session.sh` the latter
  is outside the home, under `~/.moot/sessions/<stamp>/`.
