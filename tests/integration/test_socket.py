"""Socket-level integration: real unix socket, real NDJSON streams."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from moot.core import proto
from moot.core.clock import FakeClock
from moot.core.config import Config
from moot.core.hub import new_hub
from moot.core.notify import NullNotifier
from moot.core.server import listen


@pytest.fixture
def short_home():
    # macOS limits AF_UNIX paths to 104 chars; pytest's tmp_path exceeds that.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        yield Path(d)


async def read_frame(reader: asyncio.StreamReader) -> dict:
    line = await reader.readline()
    return json.loads(line)


async def connect(path: Path, name: str, kind: str = "opencode"):
    reader, writer = await asyncio.open_unix_connection(str(path))
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
    welcome = await read_frame(reader)
    assert welcome["t"] == "welcome"
    return reader, writer


async def test_end_to_end_over_socket(short_home: Path):
    config = Config(home=short_home)
    config.notifications = False
    hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
    server = await listen(hub, config, asyncio.Lock())
    async with server:
        reader_a, writer_a = await connect(config.socket_path, "alpha")
        reader_b, writer_b = await connect(config.socket_path, "beta")

        writer_a.write(
            proto.encode(
                {
                    "t": "say",
                    "to": "beta",
                    "kind": "claim",
                    "text": "über den socket",
                    "seq": 1,
                }
            )
        )
        await writer_a.drain()
        ok = await read_frame(reader_a)
        assert ok["t"] == "ok"
        deliver = await read_frame(reader_b)
        assert deliver["t"] == "deliver"
        assert deliver["msgs"][0]["text"] == "über den socket"

        writer_a.close()
        writer_b.close()


async def test_concurrent_handles_with_lock(short_home: Path, monkeypatch):
    """Two clients writing at the same moment never sit inside hub.handle
    together: the server lock serializes them even though each handler runs
    in its own task and the hub awaits inside."""
    config = Config(home=short_home)
    config.notifications = False
    hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
    server = await listen(hub, config, asyncio.Lock())
    inner = hub.handle
    depth = 0
    max_depth = 0

    async def counting_handle(client, frame):
        nonlocal depth, max_depth
        depth += 1
        max_depth = max(max_depth, depth)
        try:
            # a real suspension point inside the critical section: without the
            # lock the other handler task enters here too and depth reaches 2
            await asyncio.sleep(0.05)
            await inner(client, frame)
        finally:
            depth -= 1

    async with server:
        reader_a, writer_a = await connect(config.socket_path, "alpha")
        reader_b, writer_b = await connect(config.socket_path, "beta")
        monkeypatch.setattr(hub, "handle", counting_handle)

        for writer, to in ((writer_a, "beta"), (writer_b, "alpha")):
            writer.write(
                proto.encode(
                    {"t": "say", "to": to, "kind": "note", "text": "hi", "seq": 1}
                )
            )
        await asyncio.gather(writer_a.drain(), writer_b.drain())
        frames = await asyncio.gather(
            asyncio.wait_for(read_frame(reader_a), 3),
            asyncio.wait_for(read_frame(reader_b), 3),
        )

        assert max_depth == 1
        assert {f["t"] for f in frames} == {"ok", "deliver"}
        assert set(hub.participants) == {"alpha", "beta"}

        writer_a.close()
        writer_b.close()


async def test_oversize_frame_rejected_and_closed(short_home: Path):
    config = Config(home=short_home)
    config.notifications = False
    hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
    server = await listen(hub, config, asyncio.Lock())
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
        writer.write(b'{"t":"say","text":"' + b"x" * (300 * 1024) + b'"}\n')
        await writer.drain()
        writer.write_eof()  # half-close: lets the server drain to a clean end
        err = await reader.readline()
        assert json.loads(err)["code"] == proto.ERR_FRAME_TOO_LARGE
        # the server drains the oversized frame, then closes: clean EOF
        assert await reader.readline() == b""
        writer.close()


async def test_oversize_frame_closed_without_client_eof(short_home: Path):
    """Same as above, but the client keeps its side open: the drain is bounded,
    so the err frame lands and the hub closes anyway (#10)."""
    config = Config(home=short_home)
    config.notifications = False
    hub = new_hub(config, clock=FakeClock(), notifier=NullNotifier())
    server = await listen(hub, config, asyncio.Lock())
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
        writer.write(b'{"t":"say","text":"' + b"x" * (300 * 1024) + b'"}\n')
        await writer.drain()
        budget = config.drain_timeout + config.close_timeout + 0.5
        err = await asyncio.wait_for(reader.readline(), budget)
        assert json.loads(err)["code"] == proto.ERR_FRAME_TOO_LARGE
        assert await asyncio.wait_for(reader.readline(), budget) == b""
        writer.close()
