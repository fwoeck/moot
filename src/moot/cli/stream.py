"""`moot stream`: the process that owns the hub connection for one session.

A runtime spoke is several processes — this one, persistent (a Claude Code
`Monitor` task or a terminal pane), plus one short-lived `moot say` / `moot
state` per tool call or hook — but a name may hold exactly one connection.
So `stream` serves the control socket (spoke/ctl.py) and relays what arrives
there to the hub, matching each reply to its `say` by `seq`.

Two threads: the reader consumes hub frames, the main thread runs the ctl
accept loop. A hub that goes away does not end the process: the reader
supervises the connection and rejoins the floor when the hub comes back, so
the session keeps its ctl socket — and its registration — across a restart.
"""

import logging
import queue
import signal
import threading
import time
from collections.abc import Callable
from functools import partial
from itertools import count
from pathlib import Path
from types import FrameType

from moot.core import proto
from moot.spoke.conn import Conn, HubError, connect
from moot.spoke.ctl import ERR_HUB_UNREACHABLE, ERR_TIMEOUT, CtlServer, ctl_path
from moot.spoke.render import rejoined_line, render

logger = logging.getLogger("moot.stream")

SAY_TIMEOUT = 10.0

# Wait between rejoin attempts; the last step repeats until the budget is out.
RECONNECT_BACKOFF: tuple[float, ...] = (1.0, 2.0, 5.0)
RECONNECT_BUDGET = 600.0

# A hello the hub refuses for a reason retrying cannot fix: another process
# holds the name, or this spoke speaks a protocol the hub does not.
FATAL_HELLO = frozenset({proto.ERR_NAME_TAKEN, proto.ERR_PROTO_MISMATCH, "malformed"})


class Stream:
    def __init__(
        self, conn: Conn, inbox: Path | None, name: str, kind: str, role: str
    ) -> None:
        self.conn = conn
        self.inbox = inbox
        # the registration this process holds: `moot brief` asks for it over
        # the ctl socket when a compacted session has forgotten its own name
        self.name = name
        self.kind = kind
        self.role = role
        self.closed = threading.Event()
        self.stopping = threading.Event()
        # set while a hub connection is usable; clear for the length of an
        # outage, which is what tells a `say` to refuse instead of blocking
        self.live = threading.Event()
        self.live.set()
        # the last state this spoke reported, replayed after every rejoin: a
        # fresh registration starts `idle` on the hub (PROTOCOL.md "hello")
        self._last_state: dict[str, object] | None = None
        self.connect: Callable[[], tuple[Conn, dict[str, object]]] | None = None
        self.backoff = RECONNECT_BACKOFF
        self.budget = RECONNECT_BUDGET
        self._pending: dict[int, queue.SimpleQueue[dict[str, object]]] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, text: str) -> None:
        """One rendered hub frame, for the model to read. The reader thread
        is the only caller."""
        if self.inbox is None:
            print(text, flush=True)
            return
        with self.inbox.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def notice(self, text: str) -> None:
        """A line about this stream rather than traffic on the floor, so
        never the inbox: `moot wait` must park a fresh session idle instead
        of handing it the join line as if someone had said something."""
        print(text, flush=True)

    def current_conn(self) -> Conn:
        """The live connection — a rejoin replaces it, so no caller may hold
        one across a call that can block."""
        with self._lock:
            return self.conn

    # ------------------------------------------------------------ hub reader

    def read_hub(self, on_eof: Callable[[], None]) -> None:
        """Read frames, and outlive the connection they arrive on.

        Every exit from `_pump` other than a rejoin is terminal: a stop, a
        hub that stays away, a hello the hub refuses, or a frame the renderer
        cannot handle (that one still propagates, and takes the process with
        it — a spoke that cannot render is not a spoke).
        """
        try:
            while True:
                self._pump()
                if self.stopping.is_set() or self.connect is None:
                    return
                if not self._reconnect():
                    return
        except Exception:
            # Logged here rather than left to the thread's excepthook: the
            # main thread exits as soon as on_eof() lands, and a daemon
            # thread's traceback is cut off mid-write when it does.
            logger.exception("the hub reader failed")
            raise
        finally:
            # Whatever ends the supervision — a stop, a spent budget, a frame
            # the renderer chokes on — the ctl server has to go with it, or
            # `moot stream` lives on serving a control socket with no hub
            # behind it and no prospect of one.
            self.closed.set()
            self.live.clear()
            self._fail_pending()
            on_eof()

    def _pump(self) -> None:
        """Frames from the current connection, until that connection ends."""
        conn = self.current_conn()
        try:
            for frame in conn.frames():
                if frame.get("t") == "ping":
                    conn.send({"t": "pong"})
                    continue
                if self._hand_to_waiter(frame):
                    continue
                for text in render(frame):
                    self.emit(text)
        finally:
            self.live.clear()
            self._fail_pending()

    def _reconnect(self) -> bool:
        """Rejoin the floor, or report why this stream is done.

        Connect failures inside the loop are the expected case — the hub is
        being restarted — so they are logged and retried rather than raised;
        a refused hello and a spent budget end the stream loudly instead.
        """
        connector = self.connect
        if connector is None:
            return False
        self.notice("[moot] hub closed — retrying for up to 10 min")
        deadline = time.monotonic() + self.budget
        for step in count():
            delay = self.backoff[min(step, len(self.backoff) - 1)]
            if self.stopping.wait(delay):
                return False
            try:
                conn, welcome = connector()
            except HubError as e:
                if e.code in FATAL_HELLO:
                    self.notice(f"[moot] cannot rejoin: {e.code} · {e.detail}")
                    return False
                logger.info("rejoin attempt failed: %s", e)
            except OSError as e:
                logger.info("rejoin attempt failed: %r", e)
            else:
                self._adopt(conn, welcome)
                return True
            if time.monotonic() >= deadline:
                self.notice("[moot] hub gone — giving up")
                return False
        raise AssertionError("unreachable")  # pragma: no cover

    def _adopt(self, conn: Conn, welcome: dict[str, object]) -> None:
        """Take the new connection over from the old one."""
        with self._lock:
            old, self.conn = self.conn, conn
            state = self._last_state
        old.close()
        if state is not None:
            conn.send(state)
        self.live.set()
        welcome["kind"] = self.kind  # the hub's welcome omits the joiner's own
        self.notice(rejoined_line(welcome))

    def _hub_gone(self) -> dict[str, object]:
        return {
            "t": "err",
            "code": ERR_HUB_UNREACHABLE,
            "detail": "the hub is gone — the stream is retrying",
        }

    def _fail_pending(self) -> None:
        """A `say` still waiting for its `ok` will never get one now."""
        with self._lock:
            waiting = list(self._pending.values())
        for slot in waiting:
            slot.put(self._hub_gone())

    def _hand_to_waiter(self, frame: dict[str, object]) -> bool:
        """An `ok`/`err` carrying a `seq` we issued belongs to a ctl client."""
        seq = frame.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return False
        with self._lock:
            slot = self._pending.get(seq)
        if slot is None:
            return False
        slot.put(frame)
        return True

    # ------------------------------------------------------------ ctl server

    def handle_ctl(self, frame: dict[str, object]) -> dict[str, object]:
        t = frame.get("t")
        if t == "state":
            return self._relay_state(frame)
        if t == "say":
            return self._forward_say(frame)
        if t == "whoami":
            # answered from the registration, so it holds after the hub is gone
            return {"t": "ok", "name": self.name, "kind": self.kind, "role": self.role}
        return {
            "t": "err",
            "code": proto.ERR_MALFORMED,
            "detail": f"ctl takes say, state and whoami frames, not {t!r}",
        }

    def _relay_state(self, frame: dict[str, object]) -> dict[str, object]:
        """A state a hook reports is never lost: it is the state this spoke
        is in, so it is cached and replayed after every rejoin."""
        with self._lock:
            self._last_state = frame
            conn = self.conn if self.live.is_set() else None
        if conn is not None:
            try:
                conn.send(frame)  # the hub sends no reply to `state`
            except OSError:
                pass  # the cached frame is replayed after the next welcome
        return {"t": "ok"}

    def _forward_say(self, frame: dict[str, object]) -> dict[str, object]:
        slot: queue.SimpleQueue[dict[str, object]] = queue.SimpleQueue()
        with self._lock:
            # `closed` is set before _fail_pending takes the lock, so a say
            # either registers early enough to be failed by it or not at all.
            if self.closed.is_set() or not self.live.is_set():
                return self._hub_gone()
            self._seq += 1
            seq = self._seq
            self._pending[seq] = slot
            conn = self.conn
        try:
            try:
                conn.send({**frame, "seq": seq})
            except OSError:
                # the connection died between the snapshot and the send; the
                # reader is already on its way into the retry loop
                return self._hub_gone()
            return slot.get(timeout=SAY_TIMEOUT)
        except queue.Empty:
            return {
                "t": "err",
                "code": ERR_TIMEOUT,
                "detail": f"the hub did not answer within {SAY_TIMEOUT:.0f}s",
            }
        finally:
            with self._lock:
                del self._pending[seq]


def run_stream(
    home: Path,
    name: str,
    kind: str,
    role: str,
    session: str,
    inbox: Path | None,
) -> int:
    conn, welcome = connect(home, name, kind, role, ["idle-events"])
    stream = Stream(conn, inbox, name, kind, role)
    # the home is resolved once, here: a rejoin must reach the same floor even
    # if `~/.moot/current` has since been pointed somewhere else
    stream.connect = partial(connect, home, name, kind, role, ["idle-events"])
    welcome["kind"] = kind  # the hub's welcome omits the joiner's own kind
    for text in render(welcome):
        stream.notice(text)

    # armed before the ctl socket exists, so no observer of the socket file
    # can SIGTERM us into the default (killing) disposition; a signal that
    # arrives before the server object does is replayed right after
    stop_requested = threading.Event()
    ctl: CtlServer | None = None

    def stop(signum: int, frame: FrameType | None) -> None:
        stop_requested.set()
        if ctl is not None:
            ctl.shutdown()  # serve_forever() then unlinks the socket and returns

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    ctl = CtlServer(ctl_path(home, session), stream.handle_ctl)
    if stop_requested.is_set():
        ctl.shutdown()
    reader = threading.Thread(target=stream.read_hub, args=(ctl.shutdown,), daemon=True)
    reader.start()
    ctl.serve_forever()

    if not stop_requested.is_set():
        # the reader gave up on the hub — the only other thing that ends the
        # accept loop, and the one exit the caller reads as a lost floor
        stream.current_conn().close()
        return 1
    stream.stopping.set()  # also wakes a retry parked between backoff steps
    conn = stream.current_conn()
    if stream.live.is_set():
        conn.send({"t": "bye"})  # the hub closes; the reader loop ends quietly
    reader.join(timeout=2.0)
    conn.close()  # shutdown-first: safe even if the reader is still parked
    return 0
