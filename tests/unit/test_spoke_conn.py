"""Conn's transport edges, driven by a socketpair and a fake hub."""

import json
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from moot.core import proto
from moot.spoke.conn import Conn, HubError, connect
from moot.spoke.observer import Pretty, Seat, _reader_loop


@pytest.fixture
def short_home() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        yield Path(d)


class FakeHub:
    """Answers exactly one hello with `reply`, or hangs up when it is None."""

    def __init__(self, home: Path, reply: dict[str, object] | None) -> None:
        self.reply = reply
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(home / "hub.sock"))
        self.listener.listen(1)
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        with conn, conn.makefile("rb") as reader:
            reader.readline()
            if self.reply is not None:
                conn.sendall(proto.encode(self.reply))

    def stop(self) -> None:
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        self.listener.close()


def test_recv_rejects_a_non_object_frame():
    ours, theirs = socket.socketpair()
    with theirs:
        conn = Conn(ours)
        theirs.sendall(b'"not a frame"\n')
        with pytest.raises(ValueError, match="non-object frame"):
            conn.recv()
        conn.close()


def test_close_unblocks_a_parked_reader():
    ours, theirs = socket.socketpair()
    conn = Conn(ours)
    frames: list[dict[str, object]] = []
    reader = threading.Thread(target=lambda: frames.extend(conn.frames()))
    reader.daemon = True
    reader.start()
    time.sleep(0.05)  # let the reader park in readline()
    closer = threading.Thread(target=conn.close, daemon=True)
    closer.start()
    closer.join(timeout=2.0)
    assert not closer.is_alive()  # close() returned instead of deadlocking
    reader.join(timeout=2.0)
    assert not reader.is_alive()  # the reader saw EOF and ended
    assert frames == []
    theirs.close()


def test_hangup_during_hello(short_home: Path):
    hub = FakeHub(short_home, None)
    try:
        with pytest.raises(HubError) as excinfo:
            connect(short_home, "alpha", "claude-code", "", [])
        assert excinfo.value.code == "closed"
    finally:
        hub.stop()


def test_unexpected_frame_instead_of_welcome(short_home: Path):
    hub = FakeHub(short_home, {"t": "ping"})
    try:
        with pytest.raises(HubError) as excinfo:
            connect(short_home, "alpha", "claude-code", "", [])
        assert excinfo.value.code == "malformed"
        assert "expected welcome" in excinfo.value.detail
    finally:
        hub.stop()


def test_reader_loop_answers_ping(capsys):
    ours, theirs = socket.socketpair()
    conn = Conn(ours)
    closed = threading.Event()
    thread = threading.Thread(
        target=_reader_loop,
        args=(
            conn,
            Pretty("frank", 80, False, False),
            closed,
            Seat("frank", Path("/nowhere")),
        ),
    )
    thread.start()
    with theirs:
        theirs.sendall(proto.encode({"t": "ping"}))
        theirs.settimeout(5)
        assert json.loads(theirs.recv(4096)) == {"t": "pong"}
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert closed.is_set()
    assert "[hub closed the connection]" in capsys.readouterr().out
    conn.close()
