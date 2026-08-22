"""The control socket: round trip, cleanup, and session-id resolution."""

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from moot.spoke.ctl import CtlServer, ctl_call, ctl_path, resolve_session


@pytest.fixture
def short_home() -> Iterator[Path]:
    # macOS limits AF_UNIX paths to 104 chars; pytest's tmp_path exceeds that.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        yield Path(d)


class Served:
    def __init__(self, home: Path, handler) -> None:
        self.calls: list[dict[str, object]] = []
        self.path = ctl_path(home, "sess-1")
        self.server = CtlServer(self.path, handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


@pytest.fixture
def served(short_home: Path) -> Iterator[Served]:
    seen: list[dict[str, object]] = []

    def handler(frame: dict[str, object]) -> dict[str, object]:
        seen.append(frame)
        if frame.get("t") == "say":
            return {"t": "ok", "id": 17, "to": frame.get("to")}
        return {"t": "err", "code": "malformed", "detail": f"{frame.get('t')}"}

    s = Served(short_home, handler)
    s.calls = seen
    try:
        yield s
    finally:
        s.stop()


def test_ctl_path(tmp_path: Path):
    assert ctl_path(tmp_path, "abc-123") == tmp_path / "ctl" / "abc-123.sock"


def test_socket_is_private_and_lives_in_ctl(served: Served, short_home: Path):
    assert served.path.parent == short_home / "ctl"
    assert stat.S_IMODE(served.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(served.path.parent.stat().st_mode) == 0o700


def test_round_trip_and_reuse(served: Served):
    reply = ctl_call(served.path, {"t": "say", "to": "beta", "text": "hi"})
    assert reply == {"t": "ok", "id": 17, "to": "beta"}
    # one frame per connection: a second call reuses the same listening socket
    assert ctl_call(served.path, {"t": "state", "state": "idle"})["code"] == "malformed"
    assert [f["t"] for f in served.calls] == ["say", "state"]


def test_unicode_survives_the_round_trip(served: Served):
    ctl_call(served.path, {"t": "say", "to": "beta", "text": "über"})
    assert served.calls[-1]["text"] == "über"


def test_client_that_sends_nothing_does_not_break_the_server(served: Served):
    with socket.socket(socket.AF_UNIX) as probe:
        probe.connect(str(served.path))
    reply = ctl_call(served.path, {"t": "say", "to": "*", "text": "still here"})
    assert reply["t"] == "ok"


def test_a_silent_client_does_not_wedge_the_server(served: Served):
    """A client that connects and never writes costs its own read deadline."""
    with socket.socket(socket.AF_UNIX) as silent:
        silent.connect(str(served.path))
        reply = ctl_call(
            served.path, {"t": "say", "to": "*", "text": "still here"}, timeout=10.0
        )
    assert reply["t"] == "ok"
    served.server.shutdown()
    served.thread.join(timeout=5)
    assert not served.thread.is_alive()


def test_client_that_hangs_up_before_reading_does_not_break_the_server(served: Served):
    with socket.socket(socket.AF_UNIX) as probe:
        probe.connect(str(served.path))
        probe.sendall(b'{"t": "say", "to": "*", "text": "and gone"}\n')
    reply = ctl_call(served.path, {"t": "say", "to": "*", "text": "still here"})
    assert reply["t"] == "ok"
    # each connection is served on its own thread, so the probe's frame may
    # still be in flight when its reply comes back: wait for both, bounded
    deadline = time.monotonic() + 5.0
    while len(served.calls) < 2:
        assert time.monotonic() < deadline, served.calls
        time.sleep(0.01)
    assert [f["t"] for f in served.calls] == ["say", "say"]


@pytest.mark.parametrize("line", [b"not json\n", b"[1, 2]\n"])
def test_a_bad_line_is_answered_and_the_server_keeps_serving(
    served: Served, line: bytes
):
    with socket.socket(socket.AF_UNIX) as probe:
        probe.connect(str(served.path))
        probe.sendall(line)
        with probe.makefile("rb") as reader:
            reply = json.loads(reader.readline())
    assert reply["t"] == "err" and reply["code"] == "malformed"
    assert served.calls == []  # the handler never saw it
    assert ctl_call(served.path, {"t": "say", "to": "*", "text": "hi"})["t"] == "ok"


def test_a_raising_handler_costs_one_client_not_the_server(short_home: Path):
    def handler(frame: dict[str, object]) -> dict[str, object]:
        if frame.get("t") == "boom":
            raise RuntimeError("handler is broken")
        return {"t": "ok"}

    served = Served(short_home, handler)
    try:
        with pytest.raises(ConnectionError, match="without a reply"):
            ctl_call(served.path, {"t": "boom"})
        assert ctl_call(served.path, {"t": "state", "state": "idle"})["t"] == "ok"
    finally:
        served.stop()


def test_shutdown_twice_is_a_no_op(short_home: Path):
    served = Served(short_home, lambda frame: {"t": "ok"})
    served.stop()
    served.server.shutdown()  # after the loop has ended: no EBADF
    assert not served.path.exists()


def test_shutdown_unlinks_the_socket(short_home: Path):
    served = Served(short_home, lambda frame: {"t": "ok"})
    assert served.path.exists()
    served.stop()
    assert not served.path.exists()


def test_missing_socket_is_reported_as_no_stream(short_home: Path):
    """`no_stream` is this process's own finding — no `moot stream` to talk
    to — as opposed to `hub_unreachable`, which a live stream reports about
    the hub behind it."""
    reply = ctl_call(ctl_path(short_home, "nobody"), {"t": "state", "state": "idle"})
    assert reply["t"] == "err"
    assert reply["code"] == "no_stream"
    assert "nobody.sock" in str(reply["detail"])


def test_dead_socket_file_is_reported_as_no_stream(short_home: Path):
    path = ctl_path(short_home, "stale")
    path.parent.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(path))  # bound but never listening: ECONNREFUSED
        reply = ctl_call(path, {"t": "state", "state": "idle"})
    assert reply["code"] == "no_stream"


def test_reply_less_server_raises(short_home: Path):
    """Anything other than a missing socket is loud, not a fallback frame."""
    path = ctl_path(short_home, "mute")
    path.parent.mkdir(mode=0o700)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)

    def accept_and_close() -> None:
        # Take the request off the wire before hanging up: closing first is a
        # race in which the client's own sendall raises EPIPE instead, which
        # is loud too but not the case under test.
        conn, _ = listener.accept()
        with conn, conn.makefile("rb") as reader:
            reader.readline()

    thread = threading.Thread(target=accept_and_close)
    thread.start()
    try:
        with pytest.raises(ConnectionError, match="without a reply"):
            ctl_call(path, {"t": "state", "state": "idle"})
    finally:
        thread.join(timeout=5)
        listener.close()


def test_a_server_that_never_answers_is_reported_as_a_frame(short_home: Path):
    """A stalled handler must not surface as a raw TimeoutError: the CLI has
    an `err <code>: <detail>` line to print, and the model reads stderr."""
    release = threading.Event()

    def handler(frame: dict[str, object]) -> dict[str, object]:
        release.wait(timeout=5)
        return {"t": "ok"}

    served = Served(short_home, handler)
    try:
        reply = ctl_call(served.path, {"t": "state", "state": "idle"}, timeout=0.3)
        assert reply["t"] == "err" and reply["code"] == "timeout"
        assert "within 0.3s" in str(reply["detail"])
    finally:
        release.set()
        served.stop()


def test_a_blocked_handler_does_not_park_another_client(short_home: Path):
    """A `say` waits on the hub for up to 10 s; the Stop hook's `moot state`
    has five. Both are ctl clients, so they must not queue behind each other."""
    entered = threading.Event()
    release = threading.Event()

    def handler(frame: dict[str, object]) -> dict[str, object]:
        if frame.get("t") == "say":
            entered.set()
            release.wait(timeout=5)
        return {"t": "ok", "for": frame.get("t")}

    served = Served(short_home, handler)
    slow = threading.Thread(
        target=lambda: ctl_call(served.path, {"t": "say", "to": "*"}, timeout=10.0)
    )
    slow.start()
    try:
        assert entered.wait(timeout=5)
        reply = ctl_call(served.path, {"t": "state", "state": "idle"}, timeout=3.0)
        assert reply == {"t": "ok", "for": "state"}
    finally:
        release.set()
        slow.join(timeout=5)
        assert not slow.is_alive()
        served.stop()


def test_resolve_session_prefers_explicit(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    assert resolve_session("explicit", read_stdin=True) == "explicit"


def test_resolve_session_reads_hook_json(tmp_path: Path, monkeypatch):
    hook = tmp_path / "hook.json"
    hook.write_text(json.dumps({"session_id": "from-stdin", "cwd": "/x"}))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with hook.open() as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        assert resolve_session(None, read_stdin=True) == "from-stdin"


def test_resolve_session_ignores_stdin_when_not_asked(tmp_path: Path, monkeypatch):
    hook = tmp_path / "hook.json"
    hook.write_text(json.dumps({"session_id": "from-stdin"}))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    with hook.open() as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        assert resolve_session(None, read_stdin=False) == "from-env"


def test_resolve_session_falls_through_empty_stdin(tmp_path: Path, monkeypatch):
    empty = tmp_path / "empty.json"
    empty.write_text("")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    with empty.open() as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        assert resolve_session(None, read_stdin=True) == "from-env"


def test_resolve_session_falls_through_hook_json_without_a_session(
    tmp_path: Path, monkeypatch
):
    hook = tmp_path / "hook.json"
    hook.write_text(json.dumps({"cwd": "/x"}))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    with hook.open() as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        assert resolve_session(None, read_stdin=True) == "from-env"


def test_resolve_session_gives_up_on_a_silent_pipe(monkeypatch):
    """A pipe nobody writes to costs one second, not the hook timeout."""
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    with os.fdopen(read_fd) as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        assert resolve_session(None, read_stdin=True) == "from-env"
    os.close(write_fd)


def resolve_within(budget: float) -> str:
    """resolve_session on a thread, so an unbounded read fails instead of hanging."""
    got: list[str] = []
    worker = threading.Thread(
        target=lambda: got.append(resolve_session(None, read_stdin=True)), daemon=True
    )
    worker.start()
    worker.join(timeout=budget)
    assert not worker.is_alive(), f"resolve_session outlived {budget}s"
    return got[0]


def test_resolve_session_reads_a_pipe_that_stays_open(monkeypatch):
    """A hook that writes its JSON without closing still answers at once."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps({"session_id": "from-pipe"}).encode())
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    with os.fdopen(read_fd) as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        assert resolve_within(0.5) == "from-pipe"  # well inside the 1 s budget
    os.close(write_fd)


def test_resolve_session_gives_up_on_a_half_written_pipe(monkeypatch):
    """Partial JSON on a pipe nobody closes costs one second, not forever."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"session_id"')
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    with os.fdopen(read_fd) as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        assert resolve_within(3.0) == "from-env"
    os.close(write_fd)


def test_resolve_session_is_loud_about_garbage_on_a_closed_stdin(
    tmp_path: Path, monkeypatch
):
    junk = tmp_path / "junk.json"
    junk.write_text("not json")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    with junk.open() as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        with pytest.raises(json.JSONDecodeError):
            resolve_session(None, read_stdin=True)


def test_resolve_session_without_anything(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with pytest.raises(ValueError, match="no session id"):
        resolve_session(None, read_stdin=False)
