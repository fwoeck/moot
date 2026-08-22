"""The control socket: one connection, several processes.

`moot stream` owns the hub connection and serves `<home>/ctl/<session>.sock`;
`moot say` and `moot state` are short-lived processes that hand it one frame
and read one reply. This is spoke-internal — the wire protocol does not know
about it (docs/SPOKE-GUIDE.md, One connection, several processes).
"""

import json
import logging
import os
import select
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from moot.core import proto

logger = logging.getLogger("moot.ctl")

ERR_HUB_UNREACHABLE = "hub_unreachable"
ERR_NO_STREAM = "no_stream"
ERR_TIMEOUT = "timeout"

# A ctl client sends its frame immediately after connecting; anything slower
# is a client that died mid-write, and it must not park the accept loop.
_READ_TIMEOUT = 2.0


def ctl_path(home: Path, session: str) -> Path:
    return home / "ctl" / f"{session}.sock"


class CtlServer:
    """One NDJSON request frame per connection, one reply frame, then close.

    Binding happens in __init__, so the socket exists the moment the object
    does; `serve_forever()` runs the accept loop until `shutdown()` is called
    from another thread. Each connection is served on its own short-lived
    thread, because a `say` handler waits for the hub's `ok` and must not
    park the `moot state` of a hook behind it.
    """

    def __init__(
        self, path: Path, handler: Callable[[dict[str, object]], dict[str, object]]
    ) -> None:
        self.path = path
        self.handler = handler
        self._closing = threading.Event()
        # reentrant: a second SIGINT can land inside the handler holding it
        self._shutdown_lock = threading.RLock()
        self._wake_r, self._wake_w = socket.socketpair()
        if not path.parent.exists():
            path.parent.mkdir(parents=True, mode=0o700)
        path.unlink(missing_ok=True)
        self._sock = socket.socket(socket.AF_UNIX)
        old_umask = os.umask(0o177)
        try:
            self._sock.bind(str(path))
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)
        self._sock.listen(8)

    def serve_forever(self) -> None:
        sel = selectors.DefaultSelector()
        sel.register(self._sock, selectors.EVENT_READ)
        sel.register(self._wake_r, selectors.EVENT_READ)
        try:
            while not self._closing.is_set():
                ready = sel.select()
                if self._closing.is_set():
                    return
                for key, _ in ready:
                    if key.fileobj is self._sock:
                        self._serve_one()
        finally:
            sel.close()
            self._sock.close()
            self._wake_r.close()
            self.path.unlink(missing_ok=True)

    def _serve_one(self) -> None:
        """Accept one connection and answer it on a thread of its own.

        The handler for a `say` blocks until the hub answers it, so serving
        connections inline would leave a `moot state` — a Claude Code hook
        with five seconds to live — waiting behind an unrelated message.
        """
        conn, _ = self._sock.accept()
        conn.settimeout(_READ_TIMEOUT)
        threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn: socket.socket) -> None:
        """Read one frame, hand it to the handler, write the reply.

        A ctl client is a separate local process, so its failures are the
        boundary this server validates: a stalled or vanished client is
        logged and dropped, a bad line is answered with an `err`. A handler
        failure stays loud — logged here, and seen by the caller as a
        connection closed without a reply — but still costs one client, not
        the control plane of a running `moot stream`.
        """
        try:
            with conn, conn.makefile("rb") as reader:
                line = reader.readline()
                if not line:
                    return  # client vanished before sending its frame
                reply = json.dumps(self._reply(line), ensure_ascii=False)
                conn.sendall(reply.encode() + b"\n")
        except OSError as e:
            logger.warning("ctl client dropped: %r", e)
        except Exception:
            logger.exception("ctl request failed")

    def _reply(self, line: bytes) -> dict[str, object]:
        try:
            frame = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return {"t": "err", "code": proto.ERR_MALFORMED, "detail": str(e)}
        if not isinstance(frame, dict):
            return {
                "t": "err",
                "code": proto.ERR_MALFORMED,
                "detail": "frame is not a JSON object",
            }
        return self.handler(frame)

    def shutdown(self) -> None:
        """Stop the accept loop; it unlinks the socket on its way out.

        Owns the wake socket, so calling this twice (or after the loop has
        ended) is a no-op rather than a write to a closed descriptor. The
        lock makes that true for the two callers that race: the hub reader
        thread on EOF and a signal handler on the main thread.
        """
        with self._shutdown_lock:
            if self._closing.is_set():
                return
            self._closing.set()
            self._wake_w.send(b"x")
            self._wake_w.close()


def ctl_call(
    path: Path, frame: dict[str, object], timeout: float = 10.0
) -> dict[str, object]:
    """Send one frame to a CtlServer and return its reply.

    Two conditions are reported as frames rather than raised, because the
    caller has an `err <code>: <detail>` line to print for them: no `moot
    stream` is running for this session, and one that does not answer in
    time. Everything else stays loud.
    """
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        sock.close()
        return {
            "t": "err",
            "code": ERR_NO_STREAM,
            "detail": f"no moot stream at {path} ({e})",
        }
    try:
        with sock, sock.makefile("rb") as reader:
            sock.sendall(json.dumps(frame, ensure_ascii=False).encode() + b"\n")
            line = reader.readline()
    except TimeoutError:
        return {
            "t": "err",
            "code": ERR_TIMEOUT,
            "detail": f"no reply from {path} within {timeout:g}s",
        }
    if not line:
        raise ConnectionError(f"{path} closed without a reply")
    reply = json.loads(line)
    if not isinstance(reply, dict):
        raise ValueError(f"ctl server sent a non-object frame: {line!r}")
    return reply


def _read_hook_json(stream: TextIO, budget: float) -> str:
    """Read one JSON object off `stream`, giving up after `budget` seconds.

    A hook writes its JSON and closes, but the stdin a command inherits can
    be a pipe that stays open with nothing — or something unfinished — in
    it, so the whole read is bounded, not just the wait for the first byte.
    A payload that never completes is discarded; one that ends at EOF is
    returned as it stands, so garbage on a hook's stdin still fails loudly.
    """
    deadline = time.monotonic() + budget
    fd = stream.fileno()
    data = b""
    while (remaining := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return ""
        chunk = os.read(fd, 65536)
        if not chunk:
            break  # EOF
        data += chunk
        try:
            json.loads(data)
        except ValueError:
            continue  # not a whole object yet
        break
    return data.decode()


def resolve_session(explicit: str | None, read_stdin: bool) -> str:
    """`--session` → hook JSON on stdin → $CLAUDE_CODE_SESSION_ID.

    The hook-run commands read stdin (`moot state`, and `moot brief` without
    `--name`; hooks pipe their JSON and close it); for `moot say` the Bash
    tool's stdin may be a pipe that never closes, so the 1 s budget is the
    guard for both.
    """
    if explicit:
        return explicit
    if read_stdin and not sys.stdin.isatty():
        data = _read_hook_json(sys.stdin, 1.0)
        if data.strip():
            payload = json.loads(data)
            if isinstance(payload, dict):
                session = payload.get("session_id")
                if isinstance(session, str) and session:
                    return session
    env = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if env:
        return env
    raise ValueError(
        "no session id: pass --session, pipe the hook JSON, "
        "or export CLAUDE_CODE_SESSION_ID"
    )
