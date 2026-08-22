"""`moot stream` internals that need no hub: the ctl frames it answers
itself, and the reconnect supervisor driven over socketpairs.

The supervisor is threads and sockets, so every wait here is bounded: a
regression must fail the suite rather than park it.
"""

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from moot.cli.stream import Stream
from moot.core import proto
from moot.spoke.conn import Conn, HubError

BUDGET = 5.0  # ceiling for every wait in this file


@pytest.fixture
def stream() -> Iterator[Stream]:
    ours, theirs = socket.socketpair()
    conn = Conn(ours)
    try:
        yield Stream(conn, None, "beta", "opencode", "refuter")
    finally:
        conn.close()
        theirs.close()


def test_whoami_answers_from_the_registration(stream: Stream):
    assert stream.handle_ctl({"t": "whoami"}) == {
        "t": "ok",
        "name": "beta",
        "kind": "opencode",
        "role": "refuter",
    }


def test_whoami_still_answers_once_the_hub_is_gone(stream: Stream):
    """`moot brief` asks after a compaction, which is exactly when the floor
    may already be gone — the registration is this process's own, so the
    answer does not depend on the hub."""
    stream.closed.set()
    assert stream.handle_ctl({"t": "whoami"})["name"] == "beta"


def test_an_unknown_ctl_frame_names_the_three_it_takes(stream: Stream):
    reply = stream.handle_ctl({"t": "roster"})
    assert reply["code"] == "malformed"
    assert "say, state and whoami" in str(reply["detail"])


# ------------------------------------------------------------ the supervisor


class Peer:
    """The hub end of one connection the stream holds."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.sock.settimeout(BUDGET)
        self.file = sock.makefile("rb")

    def send(self, frame: dict[str, object]) -> None:
        self.sock.sendall(proto.encode(frame))

    def read(self) -> dict[str, Any]:
        line = self.file.readline()
        assert line, "the stream closed its end without sending"
        return json.loads(line)

    def close(self) -> None:
        self.file.close()
        self.sock.close()


class FakeFloor:
    """`connect()` without a hub: one end of a fresh socketpair per call, or
    the failure the test has armed for the attempts that follow."""

    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.peers: list[Peer] = []
        self.attempts = 0
        self.round = 3
        self._lock = threading.Lock()

    def connect(self) -> tuple[Conn, dict[str, object]]:
        with self._lock:
            self.attempts += 1
            failure = self.failure
        if failure is not None:
            raise failure
        return self.open()

    def open(self) -> tuple[Conn, dict[str, object]]:
        spoke, hub = socket.socketpair()
        with self._lock:
            self.peers.append(Peer(hub))
        welcome: dict[str, object] = {
            "t": "welcome",
            "name": "beta",
            "round": self.round,
            "peers": [],
            "limits": {"max_rounds": 24},
        }
        return Conn(spoke), welcome

    def peer(self, index: int) -> Peer:
        with self._lock:
            return self.peers[index]

    def close_all(self) -> None:
        with self._lock:
            peers = list(self.peers)
        for peer in peers:
            peer.close()


class Supervised:
    """A `Stream` whose reader thread is running the reconnect supervisor."""

    def __init__(self, floor: FakeFloor) -> None:
        conn, _ = floor.open()
        self.floor = floor
        self.stream = Stream(conn, None, "beta", "opencode", "refuter")
        self.stream.connect = floor.connect
        self.stream.backoff = (0.01, 0.01)
        self.stream.budget = 0.2
        self.notices: list[str] = []
        self.emitted: list[str] = []
        self.stream.notice = self.notices.append
        self.stream.emit = self.emitted.append
        self.eof = threading.Event()
        self.thread = threading.Thread(
            target=self.stream.read_hub, args=(self.eof.set,), daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def until(self, what: str, pred: Callable[[], bool]) -> None:
        deadline = time.monotonic() + BUDGET
        while not pred():
            assert time.monotonic() < deadline, f"{what}: {self.notices}"
            time.sleep(0.005)

    def stop(self) -> None:
        self.stream.stopping.set()
        self.stream.current_conn().close()  # unblocks a reader parked in recv()
        self.floor.close_all()
        if self.thread.ident is not None:
            self.thread.join(timeout=BUDGET)


@pytest.fixture
def sup() -> Iterator[Supervised]:
    floor = FakeFloor()
    supervised = Supervised(floor)
    try:
        yield supervised
    finally:
        supervised.stop()


RETRY = "[moot] hub closed — retrying for up to 10 min"
REJOINED = "[moot] rejoined as beta · round 3/24 · peers: none"


def test_eof_retries_and_rejoins(sup: Supervised):
    """A hub that goes away and comes back costs the session its connection,
    not its process: the stream rejoins and reads the new one."""
    sup.stream.budget = BUDGET
    sup.start()
    sup.floor.peer(0).close()

    sup.until("the stream rejoined", lambda: sup.notices[-1:] == [REJOINED])
    assert sup.notices == [RETRY, REJOINED]
    assert sup.stream.live.is_set()
    assert not sup.eof.is_set() and not sup.stream.closed.is_set()

    sup.floor.peer(1).send(
        {
            "t": "deliver",
            "round": 4,
            "msgs": [
                {
                    "from": "alpha",
                    "to": "beta",
                    "kind": "note",
                    "text": "wieder da",
                    "addressing": "direct",
                    "id": 7,
                }
            ],
        }
    )
    sup.until("the new connection is pumped", lambda: bool(sup.emitted))
    assert sup.emitted == ["[r4 #7] alpha → beta · note: wieder da"]


def test_the_cached_state_is_resent_after_a_welcome(sup: Supervised):
    """A reclaim resets the registration to `idle` on the hub, so the state
    this spoke last reported has to be stated again."""
    sup.stream.budget = BUDGET
    sup.start()
    assert sup.stream.handle_ctl({"t": "state", "state": "busy"}) == {"t": "ok"}
    assert sup.floor.peer(0).read() == {"t": "state", "state": "busy"}

    sup.floor.peer(0).close()
    sup.until("the stream rejoined", lambda: sup.notices[-1:] == [REJOINED])
    assert sup.floor.peer(1).read() == {"t": "state", "state": "busy"}


def test_a_say_during_the_gap_is_refused(sup: Supervised):
    """A say the stream cannot deliver must come back as an error the model
    can read, not park for the length of the outage."""
    sup.stream.budget = BUDGET
    sup.floor.failure = ConnectionRefusedError("no hub yet")
    sup.start()
    sup.floor.peer(0).close()
    sup.until(
        "a rejoin attempt failed",
        lambda: not sup.stream.live.is_set() and sup.floor.attempts > 0,
    )

    reply = sup.stream.handle_ctl({"t": "say", "to": "*", "kind": "note", "text": "x"})
    assert reply == {
        "t": "err",
        "code": "hub_unreachable",
        "detail": "the hub is gone — the stream is retrying",
    }
    assert not sup.stream.closed.is_set()  # the ctl socket outlives the gap


def test_a_state_during_the_gap_is_answered_and_kept(sup: Supervised):
    """The hooks keep firing while the hub is away: each one gets its `ok`,
    and the last one is what the rejoin restates."""
    sup.stream.budget = BUDGET
    sup.floor.failure = ConnectionRefusedError("no hub yet")
    sup.start()
    sup.floor.peer(0).close()
    sup.until("the gap opened", lambda: not sup.stream.live.is_set())

    assert sup.stream.handle_ctl({"t": "state", "state": "busy"}) == {"t": "ok"}
    assert sup.stream.handle_ctl({"t": "state", "state": "idle"}) == {"t": "ok"}
    assert sup.stream._last_state == {"t": "state", "state": "idle"}


def test_a_say_whose_socket_dies_between_snapshot_and_send_is_refused():
    """The reader may not have noticed the EOF yet when a say takes its
    snapshot of the connection; the send is what finds out."""
    ours, theirs = socket.socketpair()
    theirs.close()  # the hub is gone, and this stream still thinks it is live
    stream = Stream(Conn(ours), None, "beta", "opencode", "refuter")
    assert stream.live.is_set() and not stream.closed.is_set()

    reply = stream.handle_ctl({"t": "say", "to": "*", "kind": "note", "text": "x"})
    assert reply["code"] == "hub_unreachable"
    ours.close()


def test_a_state_the_dead_socket_swallows_is_still_cached():
    """The hook that reported it is gone by the time the send fails, so the
    frame is kept for the next welcome instead of being lost with the
    socket — and the hook still sees its `ok`."""
    ours, theirs = socket.socketpair()
    theirs.close()
    stream = Stream(Conn(ours), None, "beta", "opencode", "refuter")

    assert stream.handle_ctl({"t": "state", "state": "blocked"}) == {"t": "ok"}
    assert stream._last_state == {"t": "state", "state": "blocked"}
    ours.close()


def test_name_taken_ends_the_loop(sup: Supervised):
    """Another process holds the name — retrying cannot fix that, so the
    stream says why and goes."""
    sup.stream.budget = BUDGET
    sup.floor.failure = HubError(proto.ERR_NAME_TAKEN, "beta is already on the floor")
    sup.start()
    sup.floor.peer(0).close()

    sup.thread.join(timeout=BUDGET)
    assert not sup.thread.is_alive()
    assert sup.notices == [
        RETRY,
        "[moot] cannot rejoin: name_taken · beta is already on the floor",
    ]
    assert sup.eof.is_set() and sup.stream.closed.is_set()
    assert not sup.stream.live.is_set()


def test_the_budget_is_finite(sup: Supervised):
    """A hub that never comes back must not leave a stream retrying forever
    against a home nobody will serve again. A hub that closes the connection
    during the hello is a hub coming up, so that one is retried like any
    socket error rather than treated as a refusal."""
    sup.floor.failure = HubError("closed", "hub closed the connection during hello")
    sup.start()
    sup.floor.peer(0).close()

    sup.thread.join(timeout=BUDGET)
    assert not sup.thread.is_alive()
    assert sup.notices == [RETRY, "[moot] hub gone — giving up"]
    assert sup.floor.attempts > 1  # the 0.2 s budget covers several 0.01 s steps
    assert sup.eof.is_set() and sup.stream.closed.is_set()


def test_stopping_wakes_a_parked_retry(sup: Supervised):
    """SIGTERM during an outage exits now, not after the next backoff step."""
    sup.stream.backoff = (30.0,)
    sup.stream.budget = 600.0
    sup.floor.failure = ConnectionRefusedError("no hub yet")
    sup.start()
    sup.floor.peer(0).close()
    sup.until("the retry notice", lambda: sup.notices == [RETRY])

    started = time.monotonic()
    sup.stream.stopping.set()
    sup.thread.join(timeout=BUDGET)
    assert not sup.thread.is_alive()
    assert time.monotonic() - started < BUDGET
    assert sup.notices == [RETRY]  # neither a rejoin nor a giving-up line
    assert sup.eof.is_set()


def test_a_render_failure_is_terminal_not_an_outage(sup: Supervised):
    """A frame the renderer chokes on is this spoke's bug, not the hub going
    away: it propagates, and no reconnect is attempted."""
    sup.floor.peer(0).send(
        {
            "t": "deliver",  # no `round`: render() raises KeyError on it
            "msgs": [
                {
                    "from": "alpha",
                    "to": "beta",
                    "kind": "note",
                    "text": "x",
                    "addressing": "direct",
                }
            ],
        }
    )
    with pytest.raises(KeyError):
        sup.stream.read_hub(sup.eof.set)  # in this thread, so it can be caught
    assert sup.floor.attempts == 0  # no rejoin was attempted
    assert sup.notices == []
    assert sup.eof.is_set() and sup.stream.closed.is_set()
    assert not sup.stream.live.is_set()
