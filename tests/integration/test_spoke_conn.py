"""The spoke client library against a real hub over a real socket."""

import asyncio
import io
import json
import os
import sys
import tempfile
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
from moot.spoke.conn import HubError, connect
from moot.spoke.observer import run_observer
from moot.spoke.render import render


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


async def read_frame(reader: asyncio.StreamReader) -> dict:
    return json.loads(await asyncio.wait_for(reader.readline(), 5))


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


async def test_connect_exchanges_messages(home: Path):
    conn, welcome = await asyncio.to_thread(
        connect, home, "alpha", "claude-code", "diagnosis", ["idle-events"]
    )
    reader, writer = await join_raw(home, "beta")
    try:
        assert welcome["name"] == "alpha"
        assert welcome["peers"] == []  # beta joined after alpha

        writer.write(
            proto.encode(
                {
                    "t": "say",
                    "to": "alpha",
                    "kind": "question",
                    "text": "über?",
                    "seq": 1,
                }
            )
        )
        await writer.drain()
        deliver = await asyncio.wait_for(asyncio.to_thread(conn.recv), 5)
        assert render(deliver) == ["[r1 #1] beta → alpha · question: über?"]

        await asyncio.to_thread(
            conn.send,
            {"t": "say", "to": "beta", "kind": "answer", "text": "ja", "seq": 1},
        )
        ok = await asyncio.wait_for(asyncio.to_thread(conn.recv), 5)
        assert ok["t"] == "ok" and ok["seq"] == 1
        assert (await read_frame(reader))["t"] == "ok"  # beta's own say
        assert (await read_frame(reader))["msgs"][0]["text"] == "ja"
    finally:
        conn.close()
        writer.close()


async def test_bye_ends_the_frame_iterator(home: Path):
    conn, _ = await asyncio.to_thread(connect, home, "alpha", "claude-code", "", [])
    collected: list[dict[str, object]] = []
    thread = threading.Thread(target=lambda: collected.extend(conn.frames()))
    thread.start()
    await asyncio.to_thread(conn.send, {"t": "bye"})
    await asyncio.to_thread(thread.join, 5)
    assert not thread.is_alive()
    assert collected == []
    conn.close()


async def test_name_taken_raises(home: Path):
    conn, _ = await asyncio.to_thread(connect, home, "alpha", "claude-code", "", [])
    try:
        with pytest.raises(HubError) as excinfo:
            await asyncio.to_thread(connect, home, "alpha", "opencode", "", [])
        assert excinfo.value.code == proto.ERR_NAME_TAKEN
        assert "alpha-2" in excinfo.value.detail
    finally:
        conn.close()


async def test_rejected_hello_raises(home: Path):
    with pytest.raises(HubError) as excinfo:
        await asyncio.to_thread(connect, home, "hub", "opencode", "", [])
    assert excinfo.value.code == proto.ERR_MALFORMED


async def test_missing_hub_socket_propagates(short_home: Path):
    with pytest.raises(FileNotFoundError):
        await asyncio.to_thread(connect, short_home, "alpha", "claude-code", "", [])


class Sink(io.StringIO):
    """stdout for the observer thread."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            return super().write(s)

    def text(self) -> str:
        with self._lock:
            return self.getvalue()


async def wait_for_text(sink: Sink, needle: str, budget: float = 5.0) -> None:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if needle in sink.text():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{needle!r} never printed; got:\n{sink.text()}")


async def test_observer_seat(home: Path, monkeypatch):
    read_fd, write_fd = os.pipe()
    keys = os.fdopen(write_fd, "wb", buffering=0)  # what the human types
    sink = Sink()
    reader, writer = await join_raw(home, "alpha", "claude-code")
    with os.fdopen(read_fd) as stdin, keys:
        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(sys, "stdout", sink)
        task = asyncio.create_task(
            asyncio.to_thread(run_observer, home, "frank", False, 100, False)
        )
        try:
            await wait_for_text(sink, "welcome frank")
            assert "peers alpha" in sink.text()

            keys.write(b"!question @alpha what broke?\n")
            deliver = await read_frame(reader)
            assert deliver["msgs"][0]["kind"] == "question"

            writer.write(
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
            await writer.drain()
            await wait_for_text(sink, "alpha → you answer  the index")

            keys.write(b"/roster\n")
            await wait_for_text(sink, "roster r1")
            keys.write(b"/freeze\n")
            await wait_for_text(sink, " · frozen")
            keys.write(b"/resume 3\n")
            await wait_for_text(sink, " · resumed")
            keys.write(b"!state nonsense\n")
            await wait_for_text(sink, "[ignored] !state nonsense")
            keys.write(b"\n")  # blank lines are skipped
            keys.close()  # EOF: the observer says bye and returns
            await asyncio.wait_for(task, 10)
            assert "[hub closed the connection]" in sink.text()
        finally:
            if not task.done():  # pragma: no cover
                task.cancel()
            writer.close()
