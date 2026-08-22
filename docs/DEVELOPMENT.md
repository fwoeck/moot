# Development

## Layout

```
src/moot/
  core/            # the hub — mypy --strict, ≥90% coverage target
    proto.py       # wire contract
    hub.py         # state machine (asyncio-free)
    server.py      # asyncio socket wiring
    types.py       # internal message representation (`Msg`)
    buffer.py bucket.py dedup.py transcript.py notify.py clock.py config.py
  spoke/           # the client library (stdlib only, mypy --strict)
    conn.py        # blocking protocol client
    home.py        # --home / $MOOT_HOME / ~/.moot/current / ~/.moot
    render.py      # inbound frames → text for a model
    ctl.py         # control socket (server side in `stream`, client in say/state)
    brief.py       # the operating rules, as data
    observer.py    # the human seat: seat state (message ring, peers, goal),
                   #   local commands, status band, plain line mode, TUI wiring
    tui.py         # the seat's terminal kit: editor, history, key parser, screen
  cli/
    __init__.py    # argparse: serve, observe, stream, say, state, brief,
                   #   log, doctor, wait, peek
    stream.py      # the process that owns one session's connection
    inbox.py       # wait/peek, the file bridge
    log.py         # today's transcript → text or markdown
    doctor.py      # install and session self-check, one row per check
skills/moot/       # the Claude Code spoke: SKILL.md (frontmatter hooks + body)
opencode/          # the OpenCode spoke: moot.ts (plugin factory), lib.ts
                   # (connection + fixture-pinned renderer), moot.test.ts
scripts/session.sh # launcher: tmux window with hub + observer
tests/
    conftest.py      # Harness + FakeClient/DyingClient/YieldingClient
  fixtures/
    render_cases.json  # frame → expected lines; shared by pytest and bun test
  unit/            # primitives, transcript, server layer, spoke modules, CLI parsing
  integration/     # fake spokes; socket-level tests; CLI subprocess tests
  property/        # hypothesis: protocol invariants under random interleavings
docs/
```

`opencode/moot.ts` is loaded by OpenCode from a symlink and has no build step;
`opencode/package.json` lets `bun test opencode/` resolve `@opencode-ai/plugin`
— and the symlinked plugin too: OpenCode resolves the import from the symlink
target, so `bun install` in `opencode/` is part of the symlink install (see
MANUAL-TEST.md).

## Setup and gates

```bash
uv sync                                          # creates .venv, installs dev deps
uv run pytest                                    # full suite (530 tests)
uv run pytest --cov                              # coverage (93% over all of src/moot)
uv run coverage report --include='src/moot/core/*'    # 97% on core/
uv run coverage report --include='src/moot/spoke/*'   # 96% on spoke/ (cli/ runs in subprocesses)
uv run mypy                                      # strict mode, all of moot
uv run ruff check src tests
uv run ruff format --check src tests             # or without --check to apply
```

All five should be green before committing (`--cov` has no `fail_under`; it is
the measurement README.md and this file quote, so re-run it whenever tests
change). `scripts/` holds no Python, so
`ruff check src tests scripts` is equivalent — the shell launcher is outside
ruff's reach either way.

Optional sixth gate, for changes to the OpenCode spoke:

```bash
bun test opencode/     # renderer against the shared fixture + Connection against a real hub
```

It needs `bun` and spawns `moot serve` in a short `/tmp` home with its own
`$HOME`, so it never touches your `~/.moot/current`. It is deliberately not
part of the required gates (the Python side must be testable without bun).
`opencode/lib.ts` and `src/moot/spoke/render.py` are pinned to the same
`tests/fixtures/render_cases.json`: `uv run pytest` catches a Python renderer
that drifts from the fixture, and only `bun test` catches a TypeScript one, so
run it whenever you touch the fixture or either renderer — and change the
fixture and both implementations in one commit.

## Test strategy

The hub is a state machine with concurrency; testability rests on injected
time and I/O — **no `time.sleep` in hub-logic tests**. Thread- and
socket-level tests may poll, but only inside a bounded deadline.

- `FakeClock` drives `clock.monotonic()`/`wall()`; the watchdog tick is a
  plain method call in tests (`await hub.watchdog_tick()`), wrapped in an
  asyncio task only in production.
- Hub logic is asyncio-free: `tests/conftest.py` provides a `Harness` that
  wires a `Hub` to `FakeClient`s (in-memory `send` collectors) and offers
  `join`/`say`/`state`/`cmd` helpers plus `delivered(name)` for assertions.
- Two further doubles model awkward peers, injected via `Harness.join_with`:
  `DyingClient` (send() never raises but flips `connected=False` and drops
  frames from its first `die_on` frame on) and `YieldingClient` (send() yields
  to the event loop once, so two `hub.handle()` calls interleave).
- Socket-level tests use real asyncio streams against a real unix socket.
  Note: macOS limits AF_UNIX paths to 104 chars — use short `/tmp` homes in
  those tests, not pytest's deeply nested `tmp_path`.
- The observer TUI is covered end to end in
  `tests/integration/test_observer_tui.py`: a real hub, the seat on a real
  pty (stdlib `pty`), assertions on the drawn bytes — echo-on-`ok`, delivery
  rendering, history, the local commands (`/show`, `/last`, `/find`), the
    local refusals, the `/close` macro, the private-say echo, the polled status
  band, and teardown
  escape sequences. Its seat thread is a daemon and every wait is bounded, so
  a stuck seat fails the test instead of hanging the suite. A test that needs
  the roster poll monkeypatches `observer.ROSTER_POLL_INTERVAL` (the loop
  re-reads the module global) instead of sleeping for the 3 s default.
- The CLI is tested end to end in `tests/integration/test_cli.py`: a real hub
  in the test's own event loop, a real `moot stream` subprocess, real
  `moot say`/`moot state` against its control socket. Those subprocesses run
  outside coverage.py, so `cli/stream.py` and `cli/inbox.py` show low coverage
  numbers while being exercised the hardest — read them with that in mind, and
  always bound the subprocess waits with a timeout.
- The `moot stream` internals that need no hub — the ctl frames it answers
  itself and the reconnect supervisor — are unit-tested in
  `tests/unit/test_cli_stream.py`, driven over socketpairs with every wait
  bounded, so a parked supervisor fails the suite instead of hanging it.
- `cli/log.py` and `cli/doctor.py` are unit-tested in process
  (`tests/unit/test_cli_log.py`, `tests/unit/test_cli_doctor.py`), so their
  coverage numbers are real. The log tests inject a `FakeClock` into both the
  writer and the renderer — `FakeClock.wall()` is in 1993, and a real-clock
  reader would look for another day's file. The doctor tests redirect
  `Path.home()` *and* `$HOME`, and stub `shutil.which` and the `ps` runner, so
  no test starts a process or reads the real `~/.moot`. Each command also has
  one subprocess test in `tests/integration/test_cli.py`.
- Property tests (`hypothesis`) replay random interleavings of say/state and
  assert the delivery invariants after every step:
  FIFO per (sender, recipient) pair across queue and context buffer;
  every agent-bound `deliver` contains at least one wake message;
  no message delivered twice to the same recipient;
  a message sits in queue **or** context, never both;
  a private message is never in another agent's queue, context or deliveries;
  and full no-loss accounting against the transcript (one agent copy for a
  private say, none when it went to the observer). The random op stream
  generates private says, so that accounting is exercised, not just stated.

A typical integration test is ten lines:

```python
async def test_example(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "alpha", "nur für dich")
    assert harness.delivered("alpha")[0]["addressing"] == "direct"
    assert harness.clients["beta"].frames("deliver") == []  # context only
```

## Conventions

- stdlib only in `core/` and `spoke/`; new dependencies are a decision, not a
  default. The OpenCode plugin imports nothing but `@opencode-ai/plugin` and
  `node:` builtins, and has no build step — OpenCode loads the `.ts` file as
  it is.
- Errors propagate; no silent fallbacks. Validation lives at the protocol
  boundary (`proto.py`); the hub trusts validated frames. The one deliberate
  exception is the `Client.send` contract: it must neither block the caller
  nor raise, so one participant's broken socket cannot fail another's
  `hub.handle()`. A transport failure or a full outbound queue flips
  `connected` to False (logged), and the read loop reaps the registration via
  `Hub.disconnect`.
- The normative behavioral spec for the wire is `docs/PROTOCOL.md` — if hub
  behavior changes, the doc changes in the same commit.
- Notifications and time are injectable (`Notifier`, `Clock`); production
  wiring lives in `server.py` (`serve()` picks the notifier) and `hub.new_hub`.
