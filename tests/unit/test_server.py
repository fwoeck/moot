"""Server-layer coverage: home permissions, single-instance lock, serve args,
notifier, and read-loop robustness over a real socket."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import stat
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import moot
from moot.core import proto
from moot.core.clock import FakeClock
from moot.core.config import Config
from moot.core.hub import Hub, new_hub
from moot.core.notify import NullNotifier, OsascriptNotifier
from moot.core.server import (
    _acquire_lock,
    _prepare_home,
    _prepare_transcripts,
    _rendezvous_collision,
    _watchdog_loop,
    listen,
    serve,
)
from moot.spoke.home import write_current


def test_prepare_home_creates_0700(tmp_path: Path):
    config = Config(home=tmp_path / "fresh")
    _prepare_home(config)
    mode = stat.S_IMODE(config.home.stat().st_mode)
    assert mode == 0o700


def test_prepare_home_reports_regular_file_in_the_way(tmp_path: Path, capsys):
    """A regular file where the home should be aborts with the documented
    message instead of a traceback (#31)."""
    blocker = tmp_path / "home"
    blocker.write_text("not a directory")
    config = Config(home=blocker)
    with pytest.raises(SystemExit):
        _prepare_home(config)
    assert "cannot prepare" in capsys.readouterr().err


def test_acquire_lock_single_instance(tmp_path: Path):
    config = Config(home=tmp_path)
    _prepare_home(config)
    fd = _acquire_lock(config)
    with pytest.raises(SystemExit):
        _acquire_lock(config)  # second instance refuses
    os.close(fd)


def test_lock_file_holds_pid(tmp_path: Path):
    """A stale pid from an earlier run is truncated away, not appended to."""
    config = Config(home=tmp_path)
    _prepare_home(config)
    config.lock_path.write_bytes(b"999999")
    fd = _acquire_lock(config)
    try:
        assert config.lock_path.read_bytes() == str(os.getpid()).encode()
    finally:
        os.close(fd)


def test_cli_builds_config(monkeypatch):
    from moot.cli import main
    from moot.core import server as server_mod

    captured: dict[str, Config] = {}
    monkeypatch.setattr(
        server_mod, "serve_main", lambda config: captured.update(config=config)
    )
    monkeypatch.setattr(
        sys, "argv", ["moot", "serve", "--no-notify", "--home", "/tmp/abc"]
    )
    main()
    assert captured["config"].notifications is False
    assert captured["config"].home == Path("/tmp/abc")


async def test_osascript_notifier_builds_command(monkeypatch):
    calls = []

    class FakeProc:
        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await OsascriptNotifier().notify("titel", "nachricht")
    argv = calls[0]
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    # The script is a static `on run argv` handler: no interpolated data.
    assert "nachricht" not in argv[2] and "titel" not in argv[2]
    assert argv[3] == "--"
    assert argv[4] == "nachricht" and argv[5] == "titel"
    assert len(argv) == 6


async def test_osascript_notifier_passes_quotes_verbatim(monkeypatch):
    calls = []

    class FakeProc:
        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProc()

    payload = '" & (do shell script "id") & "'
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await OsascriptNotifier().notify("titel", payload)
    argv = calls[0]
    assert argv[4] == payload  # one argv element, unchanged
    assert payload not in argv[2]


async def test_osascript_notifier_survives_missing_binary(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise OSError("no osascript")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await OsascriptNotifier().notify("t", "m")  # must not raise


async def test_read_loop_skips_empty_and_recovers_from_garbage():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
        server = await listen(hub, config, asyncio.Lock())
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
            writer.write(b"\n")  # empty line: no frame
            writer.write(b"garbage not json\n")
            writer.write(
                proto.encode(
                    {
                        "t": "hello",
                        "proto": 1,
                        "name": "alpha",
                        "kind": "opencode",
                        "caps": [],
                    }
                )
            )
            await writer.drain()
            err = json.loads(await reader.readline())
            assert err["code"] == proto.ERR_MALFORMED
            welcome = json.loads(await reader.readline())
            assert welcome["t"] == "welcome"
            writer.close()


async def test_serve_starts_accepts_and_cleans_up(tmp_path: Path, monkeypatch):
    # serve() publishes ~/.moot/current: keep the real one out of the test.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        task = asyncio.create_task(serve(config))
        try:
            for _ in range(200):
                if config.socket_path.exists():
                    break
                await asyncio.sleep(0.01)
            assert config.socket_path.exists()
            assert oct(stat.S_IMODE(config.socket_path.stat().st_mode)) == "0o600"
            reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
            writer.write(
                proto.encode(
                    {
                        "t": "hello",
                        "proto": 1,
                        "name": "alpha",
                        "kind": "opencode",
                        "caps": [],
                    }
                )
            )
            await writer.drain()
            welcome = json.loads(await reader.readline())
            assert welcome["t"] == "welcome"
            writer.close()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not config.socket_path.exists()  # cleanup ran


@contextlib.asynccontextmanager
async def running_hub(**overrides: object) -> AsyncIterator[tuple[Hub, Config]]:
    """A listening hub on a short /tmp socket path, torn down deterministically."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        for key, value in overrides.items():
            setattr(config, key, value)
        hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
        server = await listen(hub, config, asyncio.Lock())
        try:
            yield hub, config
        finally:
            server.close()
            server.close_clients()
            await server.wait_closed()


async def join(
    path: Path, name: str, kind: str = "opencode", caps: list[str] | None = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(
        proto.encode(
            {
                "t": "hello",
                "proto": 1,
                "name": name,
                "kind": kind,
                "caps": ["idle-events"] if caps is None else caps,
            }
        )
    )
    await writer.drain()
    welcome = json.loads(await asyncio.wait_for(reader.readline(), 5))
    assert welcome["t"] == "welcome"
    return reader, writer


def _say(to: str, text: str, seq: int) -> bytes:
    return proto.encode(
        {"t": "say", "to": to, "kind": "note", "text": text, "seq": seq}
    )


async def test_send_to_non_reading_peer_does_not_block_hub(caplog):
    """A peer that never reads fills its bounded outbound queue and is dropped;
    the hub keeps answering everyone else at full speed (#3, #4)."""
    async with running_hub() as (hub, config):
        _stuck_reader, stuck_writer = await join(
            config.socket_path, "stuck", kind="observer", caps=[]
        )
        frank_reader, frank_writer = await join(
            config.socket_path, "frank", kind="observer", caps=[]
        )
        payload = "x" * (20 * 1024)
        with caplog.at_level(logging.WARNING, logger="moot"):
            started = time.monotonic()
            for i in range(300):
                frank_writer.write(_say("*", payload, i + 1))
            await frank_writer.drain()
            acked = 0
            while acked < 300:
                frame = json.loads(await asyncio.wait_for(frank_reader.readline(), 5))
                # the peer_left event for `stuck` arrives on the same stream
                assert frame["t"] in ("ok", "event")
                acked += frame["t"] == "ok"
            elapsed = time.monotonic() - started
            for _ in range(300):
                if "stuck" in hub.dead:
                    break
                await asyncio.sleep(0.01)
        assert elapsed < 1.0
        assert "stuck" in hub.dead and "stuck" not in hub.participants
        assert "outbound queue full" in caplog.text
        frank_writer.close()
        stuck_writer.close()


async def test_close_of_wedged_peer_returns_immediately():
    """hub.disconnect() on a peer whose socket buffer is full returns at once;
    the abort/EOF happens in the background (#4, #10)."""
    async with running_hub() as (hub, config):
        stuck_reader, stuck_writer = await join(
            config.socket_path, "stuck", kind="observer", caps=[]
        )
        frank_reader, frank_writer = await join(
            config.socket_path, "frank", kind="observer", caps=[]
        )
        payload = "x" * (20 * 1024)
        for i in range(20):
            frank_writer.write(_say("*", payload, i + 1))
        await frank_writer.drain()
        for _ in range(20):
            assert json.loads(await asyncio.wait_for(frank_reader.readline(), 5))

        client = hub.participants["stuck"].client
        started = time.monotonic()
        await hub.disconnect(client)
        assert time.monotonic() - started < 0.05

        async def to_eof() -> None:
            try:
                while await stuck_reader.read(65536):
                    pass
            except OSError:
                pass  # ECONNRESET counts as "the peer noticed"

        await asyncio.wait_for(to_eof(), config.close_timeout + 0.5)
        frank_writer.close()
        stuck_writer.close()


async def test_dead_peer_does_not_kill_sender_over_socket():
    """beta's transport is gone; alpha's broadcast still gets an ok, gamma still
    gets the deliver, and alpha's own connection survives (#2)."""
    async with running_hub() as (hub, config):
        _frank_reader, frank_writer = await join(
            config.socket_path, "frank", kind="observer", caps=[]
        )
        alpha_reader, alpha_writer = await join(config.socket_path, "alpha")
        _beta_reader, beta_writer = await join(config.socket_path, "beta")
        gamma_reader, gamma_writer = await join(config.socket_path, "gamma")

        beta_writer.transport.abort()
        alpha_writer.write(_say("*", "an alle", 1))
        await alpha_writer.drain()

        ok = json.loads(await asyncio.wait_for(alpha_reader.readline(), 5))
        assert ok["t"] == "ok"
        deliver = json.loads(await asyncio.wait_for(gamma_reader.readline(), 5))
        assert deliver["t"] == "deliver"
        assert deliver["msgs"][0]["text"] == "an alle"

        alpha_writer.write(proto.encode({"t": "roster"}))
        await alpha_writer.drain()
        roster = json.loads(await asyncio.wait_for(alpha_reader.readline(), 5))
        assert roster["t"] == "roster"
        assert hub.participants["alpha"].client.connected is True
        for w in (frank_writer, alpha_writer, gamma_writer):
            w.close()


async def test_name_taken_reaches_the_wire():
    """The err frame sent immediately before close() must still be flushed."""
    async with running_hub() as (_hub, config):
        _reader_a, writer_a = await join(config.socket_path, "alpha")
        reader_b, writer_b = await asyncio.open_unix_connection(str(config.socket_path))
        writer_b.write(
            proto.encode(
                {
                    "t": "hello",
                    "proto": 1,
                    "name": "alpha",
                    "kind": "opencode",
                    "caps": [],
                }
            )
        )
        await writer_b.drain()
        err = json.loads(await asyncio.wait_for(reader_b.readline(), 5))
        assert err["code"] == proto.ERR_NAME_TAKEN
        assert err["detail"] == "alpha-2"
        assert await asyncio.wait_for(reader_b.readline(), 5) == b""
        writer_a.close()
        writer_b.close()


async def test_handler_exception_drops_only_that_client(monkeypatch, caplog):
    """An unexpected exception in a handler drops that connection and logs a
    traceback; every other client keeps working (#5)."""

    async def boom(self: Hub, p: object, frame: object) -> None:
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(Hub, "handle_roster", boom)
    async with running_hub() as (_hub, config):
        reader_a, writer_a = await join(config.socket_path, "alpha")
        reader_b, writer_b = await join(config.socket_path, "beta")
        with caplog.at_level(logging.ERROR, logger="moot"):
            writer_a.write(proto.encode({"t": "roster"}))
            await writer_a.drain()
            assert await asyncio.wait_for(reader_a.readline(), 5) == b""
        assert "handler exploded" in caplog.text
        assert "Traceback" in caplog.text

        writer_b.write(_say("*", "still alive", 1))
        await writer_b.drain()
        ok = json.loads(await asyncio.wait_for(reader_b.readline(), 5))
        assert ok["t"] == "ok"
        writer_a.close()
        writer_b.close()


async def test_watchdog_loop_continues_after_exception(monkeypatch, caplog):
    """One failing tick must not end the watchdog (#5)."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
        ticks = 0

        async def flaky() -> None:
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                raise RuntimeError("tick exploded")

        monkeypatch.setattr(hub, "watchdog_tick", flaky)
        with caplog.at_level(logging.ERROR, logger="moot"):
            task = asyncio.create_task(_watchdog_loop(hub, 0.01, asyncio.Lock()))
            for _ in range(200):
                if ticks >= 3:
                    break
                await asyncio.sleep(0.01)
            assert ticks >= 2
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert "watchdog tick failed" in caplog.text


def _resolved(path: str) -> str:
    """write_current stores the resolved path; /tmp is a symlink on macOS.
    Sync helper: ASYNC240 bars Path.resolve() inside async functions."""
    return os.path.realpath(path)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_signal_with_connected_client_exits_and_cleans_up(sig):
    """Ctrl-C and SIGTERM (tmux kill-pane, `kill`) with a client parked in
    readline() both run the teardown: socket file and ~/.moot/current gone."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        home = Path(d)
        expected = _resolved(d)
        socket_path = home / "hub.sock"
        current = home / ".moot" / "current"  # HOME is redirected below
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "from moot.cli import main; main()",
            "serve",
            "--home",
            str(home),
            "--no-notify",
            env={**os.environ, "HOME": str(home)},
        )
        try:
            for _ in range(300):
                if socket_path.exists() and current.exists():
                    break
                await asyncio.sleep(0.01)
            assert socket_path.exists()
            assert current.read_text().strip() == expected
            reader, writer = await join(socket_path, "alpha")
            proc.send_signal(sig)
            await asyncio.wait_for(proc.wait(), 3)
            assert not socket_path.exists()
            assert not current.exists()
            writer.close()
            assert reader is not None
        finally:
            if proc.returncode is None:  # pragma: no cover
                proc.kill()
                await proc.wait()


async def test_socket_never_world_accessible():
    """The bind runs under a restrictive umask and restores it (#37)."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
        before = os.umask(0o022)
        os.umask(before)
        server = await listen(hub, config, asyncio.Lock())
        try:
            assert stat.S_IMODE(config.socket_path.stat().st_mode) == 0o600
            after = os.umask(0o022)
            os.umask(after)
            assert after == before
        finally:
            server.close()
            server.close_clients()
            await server.wait_closed()


def _realpath(path: str | Path) -> str:
    """os.path.realpath outside an `async def` (ruff ASYNC240)."""
    return os.path.realpath(path)


# --- W15 transcripts outside the home


def test_transcripts_flag_symlinks_into_the_home(tmp_path: Path):
    """Readers only know the home, so `--transcripts DIR` leaves a symlink at
    <home>/transcripts pointing at DIR."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d), transcripts=tmp_path / "elsewhere")
        _prepare_transcripts(config)
        link = config.home / "transcripts"
        assert link.is_symlink()
        assert os.path.realpath(link) == os.path.realpath(tmp_path / "elsewhere")
        hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
        hub.transcript.append({"type": "event", "event": "probe"})
        written = list((tmp_path / "elsewhere").glob("*.jsonl"))
        assert len(written) == 1
        through_link = link / written[0].name
        assert json.loads(through_link.read_text())["event"] == "probe"


def test_transcripts_default_creates_no_symlink(tmp_path: Path):
    config = Config(home=tmp_path)
    _prepare_transcripts(config)
    assert not (tmp_path / "transcripts").is_symlink()


def test_transcripts_refuses_a_real_directory_in_the_way(tmp_path: Path, capsys):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "transcripts").mkdir()
    config = Config(home=tmp_path / "home", transcripts=tmp_path / "elsewhere")
    with pytest.raises(SystemExit):
        _prepare_transcripts(config)
    assert "is a real directory" in capsys.readouterr().err


def test_transcripts_replaces_a_stale_symlink(tmp_path: Path):
    link = tmp_path / "home" / "transcripts"
    link.parent.mkdir()
    link.symlink_to(tmp_path / "gone", target_is_directory=True)
    config = Config(home=tmp_path / "home", transcripts=tmp_path / "elsewhere")
    _prepare_transcripts(config)
    assert os.path.realpath(link) == os.path.realpath(tmp_path / "elsewhere")


# --- W12/W9 session records


async def test_session_records_bracket_the_run(tmp_path: Path, monkeypatch):
    """serve() opens the transcript with a `session` record (the round budget
    rides on it as `max_rounds`) and closes it with a matching `session_end`."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        task = asyncio.create_task(serve(config))
        try:
            for _ in range(200):
                if config.socket_path.exists():
                    break
                await asyncio.sleep(0.01)
            assert config.socket_path.exists()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        files = sorted(config.transcript_dir.glob("*.jsonl"))
        assert len(files) == 1
        records = [json.loads(line) for line in files[0].read_text().splitlines()]

    assert records[0]["type"] == "session"
    session = records[0]
    assert session["pid"] == os.getpid()
    assert session["version"] == moot.__version__
    assert session["home"] == str(Path(d))
    assert session["max_rounds"] == 24
    # dropped: session.max_rounds carries the budget
    assert not [r for r in records if r.get("event") == "config"]
    ends = [r for r in records if r["type"] == "session_end"]
    assert len(ends) == 1
    assert ends[0]["id"] == session["id"] and ends[0]["round"] == 0


# --- W10a rendezvous collision


async def test_rendezvous_collision_warns(tmp_path: Path, monkeypatch, caplog):
    """A second hub takes the rendezvous pointer over, but says so."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-a-") as a:
        # write_current records the resolved path (/private/tmp on macOS)
        resolved_a = _realpath(a)
        config_a = Config(home=Path(a))
        config_a.notifications = False
        hub_a = new_hub(config_a, clock=FakeClock(), notifier=NullNotifier())
        server_a = await listen(hub_a, config_a, asyncio.Lock())
        write_current(config_a.home)
        try:
            with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-b-") as b:
                config_b = Config(home=Path(b))
                config_b.notifications = False
                assert _rendezvous_collision(config_b.home) == Path(resolved_a)

                with caplog.at_level(logging.WARNING, logger="moot"):
                    task = asyncio.create_task(serve(config_b))
                    try:
                        for _ in range(200):
                            if config_b.socket_path.exists():
                                break
                            await asyncio.sleep(0.01)
                        assert config_b.socket_path.exists()
                    finally:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                warnings = [r.getMessage() for r in caplog.records]
                assert any(resolved_a in m and b in m for m in warnings)
        finally:
            server_a.close()
            server_a.close_clients()
            await server_a.wait_closed()


def test_rendezvous_collision_ignores_a_dead_home(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-a-") as a:
        write_current(Path(a))
        assert _rendezvous_collision(Path("/tmp/moot-nonexistent")) is None
        (Path(a) / "hub.sock").write_text("")  # a socket file, but no listener
        assert _rendezvous_collision(Path("/tmp/moot-nonexistent")) is None


async def test_rendezvous_collision_ignores_our_own_home(tmp_path: Path, monkeypatch):
    """`/tmp` and `/private/tmp` are the same directory on macOS: the compare
    is by realpath, or every hub would warn about itself."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
        server = await listen(hub, config, asyncio.Lock())
        try:
            write_current(config.home)  # writes the resolved (/private/tmp) path
            assert _rendezvous_collision(Path(d)) is None
        finally:
            server.close()
            server.close_clients()
            await server.wait_closed()


async def test_goal_file_removed_on_shutdown(tmp_path: Path, monkeypatch):
    """`<home>/goal` is session state: the teardown clears it next to the
    rendezvous file, so a fresh hub in the same home starts goal-less."""
    from moot.core.hub import write_goal

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        task = asyncio.create_task(serve(config))
        try:
            for _ in range(200):
                if config.socket_path.exists():
                    break
                await asyncio.sleep(0.01)
            assert config.socket_path.exists()
            write_goal(config.home, "find the regression")
            assert (config.home / "goal").exists()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not (config.home / "goal").exists()
