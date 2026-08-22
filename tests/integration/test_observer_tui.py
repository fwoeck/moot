"""The observer TUI end to end: a real hub, a real pty, escape bytes and all."""

import asyncio
import fcntl
import os
import pty
import struct
import sys
import tempfile
import termios
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from moot.core import proto
from moot.core.clock import FakeClock
from moot.core.config import Config
from moot.core.hub import new_hub
from moot.core.notify import NullNotifier
from moot.core.server import listen
from moot.spoke import observer
from moot.spoke.tui import Screen
from tests.integration.test_spoke_conn import join_raw, read_frame


@pytest.fixture
def short_home() -> Iterator[Path]:
    # macOS limits AF_UNIX paths to 104 chars; pytest's tmp_path exceeds that.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        yield Path(d)


@pytest.fixture
async def home(short_home: Path) -> AsyncIterator[Path]:
    config = Config(home=short_home)
    config.notifications = False
    hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
    server = await listen(hub, config, asyncio.Lock())
    async with server:
        yield short_home


class Seat:
    """The observer seat on a pty: `master` accumulates everything drawn."""

    def __init__(self, home: Path, monkeypatch, tmp_path: Path) -> None:
        # deterministic layout for _tui_available and Screen; isolated history
        monkeypatch.setenv("COLUMNS", "80")
        monkeypatch.setenv("LINES", "24")
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.setenv("HOME", str(tmp_path))  # Path.home() follows $HOME
        self.home = home
        self.monkeypatch = monkeypatch
        self.exits: list[int] = []
        self._buf = ""
        self._lock = threading.Lock()

    def _pump(self) -> None:
        while True:
            try:
                data = os.read(self.master_fd, 4096)
            except OSError:  # EIO: the slave side is gone
                return
            if not data:
                return
            with self._lock:
                self._buf += data.decode("utf-8", errors="replace")

    def text(self) -> str:
        with self._lock:
            return self._buf

    def mark(self) -> int:
        return len(self.text())

    def since(self, mark: int) -> str:
        return self.text()[mark:]

    def write(self, data: bytes) -> None:
        os.write(self.master_fd, data)

    async def wait_for(self, needle: str, mark: int = 0, budget: float = 5.0) -> str:
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            text = self.since(mark)
            if needle in text:
                return text
            await asyncio.sleep(0.02)
        raise AssertionError(f"{needle!r} never drawn; got:\n{text}")

    async def __aenter__(self) -> "Seat":
        self.master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(
            self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0)
        )
        threading.Thread(target=self._pump, daemon=True).start()
        self.stdin = os.fdopen(slave_fd, "rb", buffering=0)
        self.stdout = os.fdopen(os.dup(slave_fd), "w", encoding="utf-8")
        self.monkeypatch.setattr(sys, "stdin", self.stdin)
        self.monkeypatch.setattr(sys, "stdout", self.stdout)
        self.thread = threading.Thread(
            target=lambda: self.exits.append(
                observer.run_observer(self.home, "frank", False, None, False)
            ),
            daemon=True,  # a stuck seat must fail the test, not hang the suite
        )
        self.thread.start()
        return self

    async def __aexit__(self, *exc) -> None:
        if self.thread.is_alive():
            # failure cleanup: a Ctrl-C byte wakes the seat's select and makes
            # it quit cleanly (closing the fd would NOT wake a blocked select)
            self.write(b"\x03")
            await self.join()
        if self.thread.is_alive():  # pragma: no cover - last resort
            self.stdin.close()
            self.stdout.close()
            await self.join()
        self.stdin.close()
        self.stdout.close()

    async def join(self, budget: float = 5.0) -> None:
        # via to_thread: the in-process hub needs the event loop free to
        # process the seat's bye while we wait for its shutdown
        await asyncio.wait_for(asyncio.to_thread(self.thread.join, budget), budget + 5)


async def test_observer_tui_seat(home: Path, monkeypatch, tmp_path: Path):
    raw_reader, raw_writer = await join_raw(home, "alpha", "claude-code")
    async with Seat(home, monkeypatch, tmp_path) as seat:
        try:
            await seat.wait_for("welcome frank")

            # a submission is delivered to the agent and echoed on the hub's ok
            seat.write(b"!question @alpha what broke?\r")
            deliver = await read_frame(raw_reader)
            assert deliver["msgs"][0]["kind"] == "question"
            text = await seat.wait_for("frank → alpha question  what broke?  #")
            assert "welcome frank" in text  # echo above, not inline with input

            # the agent's answer renders in the message area
            raw_writer.write(
                proto.encode(
                    {
                        "t": "say",
                        "to": "frank",
                        "kind": "answer",
                        "text": "the index",
                        "seq": 1,
                    }
                )
            )
            await raw_writer.drain()
            await seat.wait_for("alpha → you answer  the index")

            # the typed line landed in the (isolated) global history
            history = tmp_path / ".moot" / "observer_history"
            assert history.read_text().strip() == '"!question @alpha what broke?"'

            # Ctrl-C on an empty buffer quits cleanly and tears the screen down
            seat.write(b"\x03")
            await seat.join()
            assert seat.exits == [0]
            text = await seat.wait_for("\x1b[?2004l")
            assert "\x1b[r" in text
        finally:
            raw_writer.close()
    assert seat.exits == [0]


WELCOME = {
    "t": "welcome",
    "name": "frank",
    "round": 0,
    "peers": [],
    "limits": {"rate": "6/60s", "max_rounds": 24},
}


async def test_observer_tui_reader_failure_is_loud(monkeypatch, tmp_path: Path):
    import socket

    from moot.spoke.conn import Conn

    ours, theirs = socket.socketpair()
    monkeypatch.setattr(observer, "connect", lambda *args: (Conn(ours), WELCOME))
    async with Seat(Path("/nowhere"), monkeypatch, tmp_path) as seat:
        await seat.wait_for("welcome frank")
        theirs.sendall(b"this is not a frame\n")  # reader raises JSONDecodeError
        await seat.wait_for("[reader failed")
        await seat.join()
        assert seat.exits == [1]
        assert "\x1b[r" in seat.text()  # the screen was still torn down
    theirs.close()


async def test_observer_tui_history_load_failure_degrades_loudly(
    home: Path, monkeypatch, tmp_path: Path
):
    (tmp_path / ".moot").mkdir()
    (tmp_path / ".moot" / "observer_history").mkdir()  # load: IsADirectoryError
    async with Seat(home, monkeypatch, tmp_path) as seat:
        await seat.wait_for("[history: ")  # loud once, then degrade
        seat.write(b"hi\r")  # the seat still works, history disabled
        await seat.wait_for("frank → * note  hi")
        seat.write(b"\x03")
        await seat.join()
    assert seat.exits == [0]


async def test_observer_tui_unterminated_paste_degrades_to_insert(
    home: Path, monkeypatch, tmp_path: Path
):
    async with Seat(home, monkeypatch, tmp_path) as seat:
        await seat.wait_for("welcome frank")
        seat.write(b"\x1b[200~lost paste")  # the 201~ terminator never arrives
        await seat.wait_for("lost paste", budget=8.0)  # inserted after timeout
        seat.write(b"\x15")  # ctrl_u clears the recovered text
        seat.write(b"\x03")  # ctrl_c on the empty buffer quits
        await seat.join()
    assert seat.exits == [0]


async def test_observer_tui_exits_1_when_the_hub_hangs_up(
    short_home: Path, monkeypatch, tmp_path: Path
):
    config = Config(home=short_home)
    config.notifications = False
    hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
    server = await listen(hub, config, asyncio.Lock())
    async with server:
        async with Seat(short_home, monkeypatch, tmp_path) as seat:
            await seat.wait_for("welcome frank")
            server.close()
            server.close_clients()  # drop the seat's connection mid-typing
            await seat.wait_for("[hub closed the connection]")
            await seat.join()
            assert seat.exits == [1]
            assert "\x1b[r" in seat.text()  # the screen was still torn down


async def test_observer_tui_editing_commands_and_paste(
    home: Path, monkeypatch, tmp_path: Path
):
    raw_reader, raw_writer = await join_raw(home, "alpha", "claude-code")
    async with Seat(home, monkeypatch, tmp_path) as seat:
        try:
            await seat.wait_for("welcome frank")

            # -- editing: ctrl_a/delete/ctrl_e/alt_b/ctrl_w/ctrl_k -------------
            seat.write(b"abc def")
            await asyncio.sleep(0.2)
            seat.write(b"\x01")  # ctrl_a: home
            seat.write(b"\x04")  # ctrl_d on a non-empty buffer: delete char
            seat.write(b"\x05")  # ctrl_e: end
            seat.write(b"\x1bb")  # alt_b: word left
            seat.write(b"\x17")  # ctrl_w: kill word back -> "def"
            seat.write(b"\x0b")  # ctrl_k: kill to end -> ""
            seat.write(b"first")
            seat.write(b"\x1bb\x1bf")  # alt_b: word left, alt_f: word right
            seat.write(b"\x1b[D\x1b[C")  # arrow left, then right
            seat.write(b"\r")
            deliver = await read_frame(raw_reader)
            assert deliver["msgs"][0]["text"] == "first"
            await seat.wait_for("frank → * note  first")

            async def agent_idle() -> None:
                # the raw agent declares idle-events but never reports; without
                # an idle report it stays busy and everything queues
                raw_writer.write(proto.encode({"t": "state", "state": "idle"}))
                await raw_writer.drain()

            await agent_idle()

            # -- history: Up recalls, Down returns to the stash --------------
            mark = seat.mark()
            seat.write(b"\x1b[A")  # up
            await seat.wait_for("first", mark)
            seat.write(b"\x1b[B")  # down: back to the (empty) stash
            seat.write(b"\x1b[A\x15")  # up again, ctrl_u kills the line
            await asyncio.sleep(0.2)

            # -- more editing: home/end/delete/backspace, bare ESC timeout ----
            seat.write(b"xy")
            seat.write(b"\x1b[H")  # home
            seat.write(b"\x1b[3~")  # delete the x
            seat.write(b"\x1b[F")  # end
            seat.write(b"\x7f")  # backspace the y -> ""
            seat.write(b"\x1b")  # a bare Escape is discarded after 50 ms
            await asyncio.sleep(0.15)
            seat.write(b"z\x15")  # z lands (ESC was no sequence), ctrl_u kills
            await asyncio.sleep(0.1)

            # -- Alt-Enter composes a multi-line message ----------------------
            seat.write(b"line1")
            seat.write(b"\x1b\r")  # alt-enter: newline, no submit
            seat.write(b"line2\r")
            deliver = await read_frame(raw_reader)
            assert deliver["msgs"][0]["text"] == "line1\nline2"
            await agent_idle()

            # -- bracketed paste is one message with the LF kept --------------
            seat.write(b"\x1b[200~pasted\r\ntext\x1b[201~")
            await asyncio.sleep(0.2)
            seat.write(b"\r")
            deliver = await read_frame(raw_reader)
            assert deliver["msgs"][0]["text"] == "pasted\ntext"
            await agent_idle()

            # -- /roster and an invalid !state --------------------------------
            mark = seat.mark()
            seat.write(b"/roster\r")
            await seat.wait_for("roster r3", mark)  # three wake occasions so far
            seat.write(b"!state bogus\r")
            await seat.wait_for("[ignored] !state bogus")

            # -- the seat refuses an unknown peer and an unknown command ------
            mark = seat.mark()
            seat.write(b"@nobody hi\r")
            await seat.wait_for("[no peer 'nobody'", mark)
            assert "frank → nobody" not in seat.since(mark)
            seat.write(b"\x15")  # a refusal keeps the buffer: clear it
            mark = seat.mark()
            seat.write(b"/frezee\r")
            await seat.wait_for("[unknown command", mark)
            await seat.wait_for("/frezee", mark)  # still in the input
            seat.write(b"\x15")

            # -- the too-long guard refuses and keeps the buffer --------------
            monkeypatch.setattr(observer, "_MAX_SEND_BYTES", 50)
            mark = seat.mark()
            seat.write(b"123456\r")
            await seat.wait_for("[too long — not sent]", mark)
            await seat.wait_for("123456", mark)  # still in the input
            seat.write(b"\x03")  # ctrl_c on a non-empty buffer clears it
            mark = seat.mark()
            seat.write(b"\x0c")  # ctrl_l redraws the screen
            await seat.wait_for("\x1b[2J", mark)

            # -- Ctrl-D on an empty buffer quits ------------------------------
            seat.write(b"\x04")
            await seat.join()
            assert seat.exits == [0]

            history = tmp_path / ".moot" / "observer_history"
            entries = history.read_text().splitlines()
            assert '"first"' in entries
            assert '"line1\\nline2"' in entries
            assert '"pasted\\ntext"' in entries
        finally:
            raw_writer.close()
    assert seat.exits == [0]


async def test_observer_tui_local_commands(home: Path, monkeypatch, tmp_path: Path):
    _, raw_writer = await join_raw(home, "alpha", "claude-code")
    async with Seat(home, monkeypatch, tmp_path) as seat:
        try:
            await seat.wait_for("welcome frank")
            raw_writer.write(
                proto.encode(
                    {
                        "t": "say",
                        "to": "*",
                        "kind": "claim",
                        "text": "the index is missing",
                        "seq": 1,
                    }
                )
            )
            await raw_writer.drain()
            await seat.wait_for("alpha → * claim  the index is missing")

            mark = seat.mark()
            seat.write(b"/last 1\r")
            await seat.wait_for("[end of replay]", mark)
            mark = seat.mark()
            seat.write(b"/show #1\r")  # the first message of a fresh hub
            await seat.wait_for("the index is missing", mark)
            mark = seat.mark()
            seat.write(b"/find zzz\r")
            await seat.wait_for("[no match]", mark)
            mark = seat.mark()
            seat.write(b"/find [\r")
            await seat.wait_for("[bad regex:", mark)
            seat.write(b"\x15")  # the refusal kept the buffer
            seat.write(b"\x03")
            await seat.join()
            assert seat.exits == [0]
        finally:
            raw_writer.close()
    assert seat.exits == [0]


async def test_observer_tui_private_say(home: Path, monkeypatch, tmp_path: Path):
    """`@alpha !private psst`: alpha gets the flagged message, the echo carries
    the marker, and beta's next wake carries no trace of it."""
    alpha_reader, alpha_writer = await join_raw(home, "alpha", "claude-code")
    beta_reader, beta_writer = await join_raw(home, "beta", "claude-code")
    async with Seat(home, monkeypatch, tmp_path) as seat:
        try:
            await seat.wait_for("welcome frank")
            seat.write(b"@alpha !private psst\r")
            deliver = await read_frame(alpha_reader)
            assert [
                (m["text"], m["addressing"], m.get("private")) for m in deliver["msgs"]
            ] == [("psst", "direct", True)]
            await seat.wait_for("frank → alpha note  psst  #")
            await seat.wait_for("· private")

            seat.write(b"@beta wach\r")
            deliver = await read_frame(beta_reader)
            assert [m["text"] for m in deliver["msgs"]] == ["wach"]
            seat.write(b"\x03")
            await seat.join()
            assert seat.exits == [0]
        finally:
            alpha_writer.close()
            beta_writer.close()
    assert seat.exits == [0]


async def test_observer_tui_close_macro(home: Path, monkeypatch, tmp_path: Path):
    raw_reader, raw_writer = await join_raw(home, "alpha", "claude-code")
    async with Seat(home, monkeypatch, tmp_path) as seat:
        try:
            await seat.wait_for("welcome frank")
            seat.write(b"/close we are done\r")
            deliver = await read_frame(raw_reader)
            assert deliver["msgs"][0]["kind"] == "done"
            await seat.wait_for("· reset")  # the reset that follows the say
            seat.write(b"\x03")
            await seat.join()
            assert seat.exits == [0]
        finally:
            raw_writer.close()
    assert seat.exits == [0]


async def test_observer_tui_polls_the_roster_into_the_divider(
    home: Path, monkeypatch, tmp_path: Path
):
    # no sleep anywhere: the poll interval is the only clock this test needs
    monkeypatch.setattr(observer, "ROSTER_POLL_INTERVAL", 0.2)
    _, raw_writer = await join_raw(home, "alpha", "claude-code")
    async with Seat(home, monkeypatch, tmp_path) as seat:
        try:
            await seat.wait_for("welcome frank")
            mark = seat.mark()
            text = await seat.wait_for("alpha idle", mark)
            assert Screen.DIVIDER_HINT not in text  # the band replaced the hint
            assert "@frank · r" in text  # the seat's own name leads the band
            assert "roster r" not in text  # a polled reply prints no line
            seat.write(b"\x03")
            await seat.join()
            assert seat.exits == [0]
        finally:
            raw_writer.close()
    assert seat.exits == [0]
