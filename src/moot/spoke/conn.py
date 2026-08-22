"""Blocking client side of the wire protocol (docs/PROTOCOL.md).

One `Conn` wraps one connected socket. `send()` is thread-safe (the ctl
socket and the reader thread both send); `recv()`/`frames()` belong to a
single reader.
"""

import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

from moot.core import proto


class HubError(Exception):
    """The hub answered the handshake with an `err` frame."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class Conn:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._file = sock.makefile("rb")
        self._send_lock = threading.Lock()

    def send(self, frame: dict[str, object]) -> None:
        data = proto.encode(frame)
        with self._send_lock:
            self._sock.sendall(data)

    def recv(self) -> dict[str, object] | None:
        """One frame, or None at EOF."""
        line = self._file.readline()
        if not line:
            return None
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise ValueError(f"hub sent a non-object frame: {line!r}")
        return frame

    def frames(self) -> Iterator[dict[str, object]]:
        while True:
            frame = self.recv()
            if frame is None:
                return
            yield frame

    def close(self) -> None:
        """Safe against a reader blocked in recv(): the shutdown forces that
        readline to return EOF, releasing the buffer lock file.close needs."""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already disconnected — only the unblocking matters here
        self._file.close()
        self._sock.close()


def connect(
    home: Path, name: str, kind: str, role: str, caps: list[str]
) -> tuple[Conn, dict[str, object]]:
    """Connect, register, and return the connection with its welcome frame.

    A missing hub socket raises the OSError of the connect (FileNotFoundError
    or ConnectionRefusedError); a rejected `hello` raises HubError.
    """
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(str(home / "hub.sock"))
    conn = Conn(sock)
    conn.send(
        {
            "t": "hello",
            "proto": proto.PROTO_VERSION,
            "name": name,
            "kind": kind,
            "role": role,
            "caps": caps,
        }
    )
    welcome = conn.recv()
    if welcome is None:
        conn.close()
        raise HubError("closed", "hub closed the connection during hello")
    if welcome.get("t") == "err":
        conn.close()
        raise HubError(str(welcome.get("code")), str(welcome.get("detail")))
    if welcome.get("t") != "welcome":
        conn.close()
        raise HubError("malformed", f"expected welcome, got {welcome!r}")
    return conn, welcome
