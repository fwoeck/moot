"""The `moot` CLI against a real hub: `stream` as a subprocess, `say`,
`state`, `wait` driving it through its control socket.

The hub runs in the test's event loop (as in test_socket.py); everything the
CLI does happens in child processes, so every read and wait carries a timeout
— without one a broken relay would deadlock the suite instead of failing it.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from moot.cli.stream import SAY_TIMEOUT
from moot.core import proto
from moot.core.clock import Clock, FakeClock
from moot.core.config import Config
from moot.core.hub import Hub, new_hub
from moot.core.notify import NullNotifier
from moot.core.server import listen
from moot.core.transcript import Transcript
from moot.spoke.ctl import ctl_path

MOOT = str(Path(sys.executable).parent / "moot")
SESSION = "test-sess"


class Bus:
    def __init__(self, home: Path, hub: Hub, server: asyncio.AbstractServer) -> None:
        self.home = home
        self.hub = hub
        self.server = server


@pytest.fixture
def short_home() -> Iterator[Path]:
    # macOS limits AF_UNIX paths to 104 chars; pytest's tmp_path exceeds that.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        yield Path(d)


@pytest.fixture
async def bus(short_home: Path) -> AsyncIterator[Bus]:
    config = Config(home=short_home)
    config.notifications = False
    hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
    server = await listen(hub, config, asyncio.Lock())
    try:
        yield Bus(short_home, hub, server)
    finally:
        # Never wait on clients a failed test left connected: close them.
        server.close()
        server.close_clients()
        await asyncio.wait_for(server.wait_closed(), 5)


def clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MOOT_HOME", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return env


async def run_cli(
    *args: str,
    stdin_data: bytes | None = None,
    budget: float = 15.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        MOOT,
        *args,
        stdin=(
            asyncio.subprocess.PIPE
            if stdin_data is not None
            else asyncio.subprocess.DEVNULL
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env or clean_env(),
    )
    out, err = await asyncio.wait_for(proc.communicate(stdin_data), budget)
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


@asynccontextmanager
async def stream_proc(
    home: Path, name: str, session: str, inbox: Path | None = None
) -> AsyncIterator[asyncio.subprocess.Process]:
    args = [
        "stream",
        "--home",
        str(home),
        "--name",
        name,
        "--kind",
        "claude-code",
        "--session",
        session,
    ]
    if inbox is not None:
        args += ["--inbox", str(inbox)]
    proc = await asyncio.create_subprocess_exec(
        MOOT,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=clean_env(),
    )
    try:
        yield proc
    finally:
        if proc.returncode is None:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), 5)


async def stream_line(proc: asyncio.subprocess.Process, budget: float = 5.0) -> str:
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), budget)
    return line.decode().rstrip("\n")


async def until(what: str, pred: Callable[[], bool], budget: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + budget
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} did not happen within {budget}s")


async def read_frame(reader: asyncio.StreamReader, budget: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(reader.readline(), budget))


async def join_raw(home: Path, name: str, kind: str = "opencode"):
    """A second participant, driven with plain asyncio streams."""
    reader, writer = await asyncio.open_unix_connection(str(home / "hub.sock"))
    writer.write(
        proto.encode(
            {
                "t": "hello",
                "proto": 1,
                "name": name,
                "kind": kind,
                "caps": ["idle-events"],
            }
        )
    )
    await writer.drain()
    assert (await read_frame(reader))["t"] == "welcome"
    return reader, writer


class StubHub:
    """A hub socket that answers the handshake and then does only what the
    test tells it to — the two things the real hub never does: leave a `say`
    unanswered, and send a frame the renderer cannot handle."""

    def __init__(self) -> None:
        self.connected = asyncio.Event()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.server: asyncio.AbstractServer | None = None

    async def serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        hello = await read_frame(reader)
        assert hello["t"] == "hello"
        writer.write(
            proto.encode(
                {
                    "t": "welcome",
                    "name": hello["name"],
                    "round": 0,
                    "peers": [],
                    "limits": {"max_rounds": 24},
                }
            )
        )
        await writer.drain()
        self.reader, self.writer = reader, writer
        self.connected.set()

    async def send(self, frame: dict[str, object]) -> None:
        assert self.writer is not None
        self.writer.write(proto.encode(frame))
        await self.writer.drain()

    async def go_away(self) -> None:
        """Drop the connection and stop listening, so the stream's retries
        find nothing — the outage a restart does not end."""
        assert self.server is not None and self.writer is not None
        self.server.close()
        self.writer.close()


@asynccontextmanager
async def stub_hub(home: Path) -> AsyncIterator[StubHub]:
    stub = StubHub()
    server = await asyncio.start_unix_server(stub.serve, path=str(home / "hub.sock"))
    stub.server = server
    try:
        yield stub
    finally:
        # wait_closed() waits for live connections too, and this stub never
        # closes one on its own
        if stub.writer is not None and not stub.writer.is_closing():
            stub.writer.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)


async def streaming(home: Path, proc: asyncio.subprocess.Process) -> Path:
    """The join line off the stream's stdout, and its servable ctl socket."""
    assert (await stream_line(proc)).startswith("[moot] joined as ")
    ctl = ctl_path(home, SESSION)
    await until("the ctl socket exists", ctl.exists)
    return ctl


async def joined(bus: Bus, proc: asyncio.subprocess.Process, name: str) -> str:
    """The stream's join line, once its control socket is servable."""
    line = await stream_line(proc)
    await until(f"{name} registered", lambda: name in bus.hub.participants)
    await until(
        "the ctl socket exists", lambda: (bus.home / "ctl" / f"{SESSION}.sock").exists()
    )
    return line


# --------------------------------------------------------------------- say


async def test_say_reaches_a_peer(bus: Bus):
    reader, writer = await join_raw(bus.home, "beta")
    async with stream_proc(bus.home, "alpha", SESSION) as proc:
        line = await joined(bus, proc, "alpha")
        assert line == (
            "[moot] joined as alpha (claude-code) · peers: beta (opencode, idle)"
            " · round 0/24"
        )

        rc, out, err = await run_cli(
            "say",
            "--home",
            str(bus.home),
            "--session",
            SESSION,
            "@beta",
            "--kind",
            "question",
            "wo",
            "klemmt",
            "es?",
        )
        assert (rc, err) == (0, "")
        assert out.strip() == "ok → beta · #1"

        deliver = await read_frame(reader)
        assert deliver["t"] == "deliver"
        assert deliver["msgs"][0]["text"] == "wo klemmt es?"
        assert deliver["msgs"][0]["kind"] == "question"
        assert deliver["msgs"][0]["addressing"] == "direct"
    writer.close()


async def test_private_say_reaches_only_the_peer(bus: Bus):
    """End to end over the real CLI, stream and hub: the addressee and the
    observer get the flagged message, and a third agent's next wake carries no
    trace of it."""
    beta_reader, beta_writer = await join_raw(bus.home, "beta")
    gamma_reader, gamma_writer = await join_raw(bus.home, "gamma")
    frank_reader, frank_writer = await join_raw(bus.home, "frank", "observer")
    async with stream_proc(bus.home, "alpha", SESSION) as proc:
        await joined(bus, proc, "alpha")

        rc, out, err = await run_cli(
            "say",
            "--home",
            str(bus.home),
            "--session",
            SESSION,
            "--private",
            "@beta",
            "--kind",
            "claim",
            "psst",
        )
        assert (rc, err) == (0, "")
        assert out.strip() == "ok → beta · #1 · private"

        deliver = await read_frame(beta_reader)
        assert deliver["t"] == "deliver"
        assert [
            (m["text"], m["addressing"], m.get("private")) for m in deliver["msgs"]
        ] == [("psst", "direct", True)]
        frame = await read_frame(frank_reader)
        while frame["t"] != "deliver":  # peer_joined events precede it
            frame = await read_frame(frank_reader)
        assert [
            (m["text"], m["addressing"], m.get("private")) for m in frame["msgs"]
        ] == [("psst", "overheard", True)]

        rc, out, err = await run_cli(
            "say", "--home", str(bus.home), "--session", SESSION, "@gamma", "wach"
        )
        assert (rc, err) == (0, "")
        deliver = await read_frame(gamma_reader)
        assert deliver["t"] == "deliver"
        assert [m["text"] for m in deliver["msgs"]] == ["wach"]
    beta_writer.close()
    gamma_writer.close()
    frank_writer.close()


async def test_say_without_a_stream_exits_one_with_a_hint(bus: Bus):
    rc, out, err = await run_cli(
        "say", "--home", str(bus.home), "--session", "nobody", "@beta", "hallo"
    )
    assert rc == 1
    assert out == ""
    assert err.strip() == "moot: no moot stream for session nobody"


# ------------------------------------------------------------------- brief


async def test_brief_without_a_name_asks_the_stream(bus: Bus):
    """After a compaction the hook has only the session id, so the brief gets
    its name from the `moot stream` that holds the registration."""
    async with stream_proc(bus.home, "alpha", SESSION) as proc:
        await joined(bus, proc, "alpha")

        rc, out, err = await run_cli(
            "brief",
            "--home",
            str(bus.home),
            "--session",
            SESSION,
            "--runtime",
            "claude-code",
        )
        assert (rc, err) == (0, "")
        assert "You are `alpha`" in out
        assert "your floor connection is gone" not in out


# ------------------------------------------------------------------- state


async def test_state_idle_from_a_hook_flushes_the_queue(bus: Bus):
    reader, writer = await join_raw(bus.home, "beta")
    async with stream_proc(bus.home, "alpha", SESSION) as proc:
        await joined(bus, proc, "alpha")

        rc, _, err = await run_cli(
            "state", "--home", str(bus.home), "--session", SESSION, "busy"
        )
        assert (rc, err) == (0, "")
        await until(
            "alpha is busy", lambda: bus.hub.participants["alpha"].state == "busy"
        )

        writer.write(
            proto.encode(
                {
                    "t": "say",
                    "to": "alpha",
                    "kind": "question",
                    "text": "brauche die zahlen",
                    "seq": 1,
                }
            )
        )
        await writer.drain()
        ok = await read_frame(reader)
        assert ok["t"] == "ok" and ok["queued"] == 1  # alpha is busy: not delivered

        # The Stop hook's shape: no --session, the id arrives as JSON on stdin.
        rc, _, err = await run_cli(
            "state",
            "--home",
            str(bus.home),
            "idle",
            stdin_data=json.dumps(
                {"session_id": SESSION, "hook_event_name": "Stop"}
            ).encode(),
        )
        assert (rc, err) == (0, "")
        assert await stream_line(proc) == (
            "[r1 #1] beta → alpha · question: brauche die zahlen"
        )
    writer.close()


# ------------------------------------------------------- inbox file bridge


async def test_inbox_bridge_wait_returns_lines_and_reports_busy(bus: Bus):
    inbox = bus.home / "alpha.in"
    reader, writer = await join_raw(bus.home, "beta")
    async with stream_proc(bus.home, "alpha", SESSION, inbox=inbox) as proc:
        assert await stream_line(proc) == (
            "[moot] joined as alpha (claude-code) · peers: beta (opencode, idle)"
            " · round 0/24"
        )
        await until("alpha registered", lambda: "alpha" in bus.hub.participants)
        await until(
            "the ctl socket exists",
            lambda: (bus.home / "ctl" / f"{SESSION}.sock").exists(),
        )

        wait_args = (
            "wait",
            "--home",
            str(bus.home),
            "--session",
            SESSION,
            "--inbox",
            str(inbox),
            "--timeout",
            "1",
        )
        # The join line went to the stream's stdout, not into the inbox: the
        # first wait parks the session idle instead of handing it a non-message
        # and marking it busy while the floor is queueing for it.
        rc, out, err = await run_cli(*wait_args)
        assert (rc, err) == (1, "")
        assert out.strip() == "(no new messages within 1s — run wait again)"
        assert not inbox.exists()
        assert bus.hub.participants["alpha"].state == "idle"

        writer.write(
            proto.encode(
                {
                    "t": "say",
                    "to": "alpha",
                    "kind": "question",
                    "text": "brauche die zahlen",
                    "seq": 1,
                }
            )
        )
        await writer.drain()
        ok = await read_frame(reader)
        # delivered rather than queued: the timed-out wait left alpha idle
        assert ok["t"] == "ok" and ok["queued"] == 0
        await until("the delivery reached the inbox", inbox.exists)

        rc, out, err = await run_cli(*wait_args)
        assert (rc, err) == (0, "")
        assert out.strip() == "[r1 #1] beta → alpha · question: brauche die zahlen"
        assert bus.hub.participants["alpha"].state == "busy"

        rc, out, err = await run_cli(
            "peek",
            "--home",
            str(bus.home),
            "--session",
            SESSION,
            "--inbox",
            str(inbox),
            "--settle",
            "0.3",
        )
        assert (rc, err) == (1, "")
        assert out.strip() == "(nothing new)"
    writer.close()


# ------------------------------------------------------- a hub that stalls


async def test_a_hub_that_never_answers_a_say_yields_err_timeout(short_home: Path):
    """`moot say` must outlast the stream's own window on the hub, or the
    designed `err timeout` is unreachable and the model gets a traceback.
    A `moot state` alongside it must not queue behind the pending say."""
    async with stub_hub(short_home) as hub:
        async with stream_proc(short_home, "alpha", SESSION) as proc:
            await streaming(short_home, proc)
            await asyncio.wait_for(hub.connected.wait(), 5)

            say = asyncio.create_task(
                run_cli(
                    "say",
                    "--home",
                    str(short_home),
                    "--session",
                    SESSION,
                    "@beta",
                    "hallo",
                    budget=SAY_TIMEOUT + 10,
                )
            )
            assert hub.reader is not None
            assert (await read_frame(hub.reader))["t"] == "say"

            # the Stop hook's 5 s, while the say still holds the ctl socket
            rc, out, err = await run_cli(
                "state", "--home", str(short_home), "--session", SESSION, "idle"
            )
            assert (rc, err) == (0, "")

            rc, out, err = await asyncio.wait_for(say, SAY_TIMEOUT + 10)
            assert (rc, out) == (1, "")
            # the stream's own window is the one that fires, not the client's
            assert err.strip() == "err timeout: the hub did not answer within 10s"

            # a hub that never answers `bye` must not hold the exit either
            proc.terminate()
            assert await asyncio.wait_for(proc.wait(), 5) == 0


async def test_says_during_the_gap_are_refused_and_the_stream_holds_the_seat(
    short_home: Path,
):
    """A hub that goes away fails the say in flight at once and refuses the
    ones that follow — while the stream itself stays up, retrying, so the
    session keeps its control socket and its name."""
    async with stub_hub(short_home) as hub:
        async with stream_proc(short_home, "alpha", SESSION) as proc:
            ctl = await streaming(short_home, proc)
            await asyncio.wait_for(hub.connected.wait(), 5)

            say = asyncio.create_task(
                run_cli(
                    "say",
                    "--home",
                    str(short_home),
                    "--session",
                    SESSION,
                    "@beta",
                    "hallo",
                    budget=SAY_TIMEOUT + 10,
                )
            )
            assert hub.reader is not None and hub.writer is not None
            assert (await read_frame(hub.reader))["t"] == "say"
            await hub.go_away()

            # well inside the stream's own SAY_TIMEOUT: the reader fails the
            # waiter on EOF instead of leaving it to expire
            rc, out, err = await asyncio.wait_for(say, SAY_TIMEOUT - 3)
            assert (rc, out) == (1, "")
            assert err.strip() == (
                "err hub_unreachable: the hub is gone — the stream is retrying"
            )
            assert (
                await stream_line(proc)
                == "[moot] hub closed — retrying for up to 10 min"
            )

            # a say sent during the gap is answered, not parked
            rc, out, err = await run_cli(
                "say", "--home", str(short_home), "--session", SESSION, "@beta", "noch"
            )
            assert (rc, out) == (1, "")
            assert err.strip() == (
                "err hub_unreachable: the hub is gone — the stream is retrying"
            )
            assert ctl.exists() and proc.returncode is None

            proc.terminate()
            assert await asyncio.wait_for(proc.wait(), 5) == 0


async def test_a_frame_the_renderer_rejects_still_tears_the_stream_down(
    short_home: Path,
):
    """The reader thread is the only thing holding the ctl socket open. If it
    dies without a hub, `moot stream` must not sit there serving it."""
    async with stub_hub(short_home) as hub:
        async with stream_proc(short_home, "alpha", SESSION) as proc:
            ctl = await streaming(short_home, proc)
            await asyncio.wait_for(hub.connected.wait(), 5)
            await hub.send(
                {
                    "t": "deliver",
                    # no `round`: render() raises KeyError on it
                    "msgs": [
                        {
                            "from": "beta",
                            "to": "alpha",
                            "kind": "note",
                            "text": "x",
                            "addressing": "direct",
                        }
                    ],
                }
            )
            assert await asyncio.wait_for(proc.wait(), 5) == 1
            assert not ctl.exists()
            assert proc.stderr is not None
            err = (await proc.stderr.read()).decode()
            assert "the hub reader failed" in err and "KeyError" in err


# ---------------------------------------------------------------- lifecycle


async def test_hub_shutdown_starts_a_retry_and_keeps_the_ctl_socket(bus: Bus):
    """A hub restart must not cost the session its seat: the stream says it
    is retrying and goes on serving the control socket the hooks use."""
    ctl = bus.home / "ctl" / f"{SESSION}.sock"
    async with stream_proc(bus.home, "alpha", SESSION) as proc:
        await joined(bus, proc, "alpha")
        bus.server.close()
        bus.server.close_clients()
        assert (
            await stream_line(proc) == "[moot] hub closed — retrying for up to 10 min"
        )

        await asyncio.sleep(1.0)  # past the first backoff step
        assert ctl.exists() and proc.returncode is None
        proc.terminate()
        assert await asyncio.wait_for(proc.wait(), 5) == 0


async def test_the_stream_rejoins_a_restarted_hub(bus: Bus):
    """The point of the retry loop: `moot serve` comes back on the same home
    and the session is on the floor again, without restarting the session."""
    async with stream_proc(bus.home, "alpha", SESSION) as proc:
        await joined(bus, proc, "alpha")
        bus.server.close()
        bus.server.close_clients()
        assert (
            await stream_line(proc) == "[moot] hub closed — retrying for up to 10 min"
        )

        config = Config(home=bus.home)
        config.notifications = False
        hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
        server = await listen(hub, config, asyncio.Lock())
        try:
            line = await stream_line(proc, budget=15.0)
            assert re.match(
                r"^\[moot\] rejoined as alpha · round \d+/24 · peers:", line
            )
            await until("alpha registered", lambda: "alpha" in hub.participants)

            reader, writer = await join_raw(bus.home, "beta")
            rc, out, err = await run_cli(
                "say", "--home", str(bus.home), "--session", SESSION, "@beta", "wieder"
            )
            assert (rc, err) == (0, "")
            assert out.strip() == "ok → beta · #1"
            assert (await read_frame(reader))["t"] == "deliver"
            writer.close()
        finally:
            server.close()
            server.close_clients()
            await asyncio.wait_for(server.wait_closed(), 5)


async def test_sigterm_exits_zero_and_removes_the_ctl_socket(bus: Bus):
    ctl = bus.home / "ctl" / f"{SESSION}.sock"
    async with stream_proc(bus.home, "alpha", SESSION) as proc:
        await joined(bus, proc, "alpha")
        assert ctl.exists()
        proc.terminate()
        assert await asyncio.wait_for(proc.wait(), 5) == 0
    assert not ctl.exists()
    # the clean exit says `bye`, so the hub drops the registration too
    await until("alpha deregistered", lambda: "alpha" not in bus.hub.participants)


# ----------------------------------------------------------- log and doctor


async def test_log_renders_a_hand_written_transcript(short_home: Path):
    """A real `Clock()` on both sides: `moot log` reads *today's* file, and the
    `bus` fixture's FakeClock writes into 1993."""
    transcript = Transcript(short_home / "transcripts", Clock())
    transcript.append(
        {
            "type": "msg",
            "id": 4,
            "round": 2,
            "from": "beta",
            "to": "alpha",
            "kind": "objection",
            "text": "the lock is not held",
            "ts": time.time(),
        }
    )

    rc, out, err = await run_cli("log", "--home", str(short_home))

    assert (rc, err) == (0, "")
    assert out.endswith("[r2 #4] beta → alpha · objection: the lock is not held\n")


async def test_doctor_roster_joins_and_leaves(bus: Bus, tmp_path: Path):
    env = {**clean_env(), "HOME": str(tmp_path)}  # never read the real ~/.moot

    rc, out, err = await run_cli(
        "doctor", "--home", str(bus.home), "--roster", env=env, budget=45.0
    )

    assert (rc, err) == (0, "")
    assert "[roster] doctor-" in out
    assert not [n for n in bus.hub.participants if n.startswith("doctor-")]
    assert [n for n in bus.hub.dead if n.startswith("doctor-")]
