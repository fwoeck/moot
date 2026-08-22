"""Asyncio wiring for the hub: unix socket, NDJSON framing, watchdog task.

All state lives in Hub; this module only moves bytes. Single instance via
flock on ~/.moot/hub.lock; socket 0600, home 0700 — that *is* the auth
model (see docs/OPERATIONS.md "State directory layout").
"""

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import stat
import sys
from asyncio.exceptions import LimitOverrunError
from pathlib import Path

import moot
from moot.core import proto
from moot.core.config import Config
from moot.core.hub import Hub, clear_goal, new_hub
from moot.core.notify import NullNotifier, OsascriptNotifier
from moot.core.types import Client
from moot.spoke.home import (
    clear_current,
    current_file,
    hub_alive,
    write_current,
)

logger = logging.getLogger("moot")


class StreamClient(Client):
    """One connection. send() never blocks the hub and never raises: frames
    go through a bounded queue drained by a writer task; any transport
    failure or overflow flips connected=False and aborts the transport, and
    the read loop then reaps the registration via hub.disconnect()."""

    def __init__(
        self, writer: asyncio.StreamWriter, queue_max: int, close_timeout: float
    ) -> None:
        super().__init__()
        self.writer = writer
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_max)
        self._close_timeout = close_timeout
        self._writer_task = asyncio.create_task(self._write_loop())
        self._finalizer: asyncio.Task[None] | None = None

    async def send(self, frame: dict[str, object]) -> None:
        if not self.connected:
            return
        try:
            self._queue.put_nowait(proto.encode(frame))
        except asyncio.QueueFull:
            logger.warning(
                "%s: outbound queue full (%d frames) — dropping connection",
                self.name or "<unregistered>",
                self._queue.maxsize,
            )
            self._abort()

    async def _write_loop(self) -> None:
        try:
            while True:
                data = await self._queue.get()
                self.writer.write(data)
                await self.writer.drain()
        except OSError as e:  # CancelledError is BaseException: passes through
            logger.info(
                "%s: write failed (%s) — dropping connection",
                self.name or "<unregistered>",
                e,
            )
            self._abort()

    def _abort(self) -> None:
        self.connected = False
        self.writer.transport.abort()

    async def close(self) -> None:
        """Non-blocking from the hub's point of view: the bounded wait for a
        clean close runs in its own task; a peer that keeps the socket open
        is aborted after close_timeout. Frames still queued are handed to
        the transport first — send() followed immediately by close() (err
        name_taken, err frame_too_large) must reach the wire."""
        self.connected = False
        if self.writer.is_closing():
            self._writer_task.cancel()
            return
        while not self._queue.empty():
            self.writer.write(self._queue.get_nowait())
        self._writer_task.cancel()
        self.writer.close()  # transport flushes its buffer, then EOF
        self._finalizer = asyncio.create_task(self._finalize())  # keep a reference

    async def _finalize(self) -> None:
        try:
            async with asyncio.timeout(self._close_timeout):
                await self.writer.wait_closed()
        except (TimeoutError, OSError):
            self.writer.transport.abort()


async def _read_loop(
    hub: Hub,
    reader: asyncio.StreamReader,
    client: StreamClient,
    config: Config,
    lock: asyncio.Lock,
) -> None:
    try:
        while True:
            try:
                line = await reader.readline()
            except (LimitOverrunError, ValueError):
                await client.send(
                    {
                        "t": "err",
                        "code": proto.ERR_FRAME_TOO_LARGE,
                        "detail": f"frame exceeds {config.max_frame_bytes} bytes",
                        "seq": None,
                    }
                )
                # Give the client a bounded chance to read the err before the
                # close (an unread oversized frame would otherwise RST it).
                budget = config.max_frame_bytes
                try:
                    async with asyncio.timeout(config.drain_timeout):
                        while budget > 0:
                            chunk = await reader.read(min(65536, budget))
                            if not chunk:
                                break
                            budget -= len(chunk)
                except (TimeoutError, OSError):
                    pass
                break
            if not line:
                break  # peer closed
            if not line.strip():
                continue  # empty line: no frame
            try:
                frame = proto.parse_line(line)
            except proto.ValidationError as e:
                await client.send(
                    {"t": "err", "code": e.code, "detail": e.detail, "seq": None}
                )
                continue
            try:
                async with lock:
                    await hub.handle(client, frame)
            except Exception:
                logger.exception(
                    "%s: unhandled error while processing %r — dropping connection",
                    client.name or "<unregistered>",
                    frame.get("t"),
                )
                break
            if not client.connected:
                break
    except OSError:
        pass
    finally:
        async with lock:
            await hub.disconnect(client)  # idempotent: no-op if not registered


async def _watchdog_loop(hub: Hub, tick: float, lock: asyncio.Lock) -> None:
    while True:
        await asyncio.sleep(tick)
        try:
            async with lock:
                await hub.watchdog_tick()
        except Exception:
            logger.exception("watchdog tick failed — continuing")


def _prepare_home(config: Config) -> None:
    home = config.home
    try:
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        mode = stat.S_IMODE(home.stat().st_mode)
    except OSError as e:
        print(f"moot: cannot prepare {home} ({e}) — aborting", file=sys.stderr)
        sys.exit(1)
    if mode != 0o700:
        print(
            f"moot: {home} has mode {mode:04o}, expected 0700 — aborting",
            file=sys.stderr,
        )
        sys.exit(1)


def _prepare_transcripts(config: Config) -> None:
    """`--transcripts DIR`: create it and leave a symlink at <home>/transcripts
    so every reader that knows the home finds the day's files."""
    target = config.transcript_dir
    link = config.home / "transcripts"
    if os.path.realpath(target) == os.path.realpath(link):
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        # is_symlink() before exists(): a dangling link is not "existing".
        if link.is_symlink():
            if os.path.realpath(link) == os.path.realpath(target):
                return
            link.unlink()
        elif link.exists():
            print(
                f"moot: {link} is a real directory — move it aside or drop "
                "--transcripts; aborting",
                file=sys.stderr,
            )
            sys.exit(1)
        link.symlink_to(target, target_is_directory=True)
    except OSError as e:
        print(f"moot: cannot prepare {target} ({e}) — aborting", file=sys.stderr)
        sys.exit(1)


def _record_session_start(hub: Hub, config: Config) -> str:
    started = hub.clock.wall()
    session_id = f"{int(started)}-{os.getpid()}"
    hub.transcript.append(
        {
            "type": "session",
            "id": session_id,
            "started": started,
            "pid": os.getpid(),
            "version": moot.__version__,
            "home": str(config.home),
            "max_rounds": config.max_rounds,
        }
    )
    return session_id


def _record_session_end(hub: Hub, session_id: str) -> None:
    hub.transcript.append(
        {
            "type": "session_end",
            "id": session_id,
            "ts": hub.clock.wall(),
            "round": hub.round,
        }
    )


def _rendezvous_collision(home: Path) -> Path | None:
    """The home named by ~/.moot/current when that is a *live* other hub."""
    try:
        recorded = current_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not recorded:
        return None
    if os.path.realpath(recorded) == os.path.realpath(home):
        return None
    return Path(recorded) if hub_alive(Path(recorded), timeout=0.2) else None


def _acquire_lock(config: Config) -> int:
    fd = os.open(config.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        print(
            f"moot: another hub instance is running (lock: {config.lock_path})",
            file=sys.stderr,
        )
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


async def listen(
    hub: Hub, config: Config, lock: asyncio.Lock
) -> asyncio.AbstractServer:
    """Bind the unix socket and serve the hub. Shared by serve() and tests."""

    async def on_connect(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = StreamClient(writer, config.outbound_queue_max, config.close_timeout)
        await _read_loop(hub, reader, client, config, lock)

    socket_path = config.socket_path
    socket_path.unlink(missing_ok=True)
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(
            on_connect, path=str(socket_path), limit=config.max_frame_bytes
        )
    finally:
        os.umask(old_umask)
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        server.close()
        socket_path.unlink(missing_ok=True)
        raise
    return server


async def serve(config: Config) -> None:
    _prepare_home(config)
    lock_fd = _acquire_lock(config)
    # Transcript.__init__ mkdirs the directory, so the symlink must exist first.
    _prepare_transcripts(config)
    notifier = OsascriptNotifier() if config.notifications else NullNotifier()
    hub = new_hub(config, notifier=notifier)
    session_id = _record_session_start(hub, config)
    lock = asyncio.Lock()
    try:
        server = await listen(hub, config, lock)
    except OSError as e:
        os.close(lock_fd)
        print(
            f"moot: cannot bind {config.socket_path} ({e}) — aborting",
            file=sys.stderr,
        )
        sys.exit(1)
    other = _rendezvous_collision(config.home)
    if other is not None:
        logger.warning(
            "~/.moot/current points at a live hub in %s — overwriting it with %s; "
            "spokes started without --home will now find this hub",
            other,
            config.home,
        )
    write_current(config.home)  # rendezvous: spokes find this home without a path
    watchdog = asyncio.create_task(_watchdog_loop(hub, config.watchdog_tick, lock))
    logger.info(
        "listening on %s · max_rounds=%d · transcripts=%s",
        config.socket_path,
        config.max_rounds,
        config.transcript_dir,
    )
    serving = asyncio.create_task(server.serve_forever())
    # SIGTERM (tmux kill-pane, `kill`) must run the same teardown as Ctrl-C;
    # asyncio.run() only translates SIGINT. asyncio.wait() leaves `serving`
    # uncancelled when this coroutine is cancelled, so the clients are closed
    # below before serve_forever() is allowed to wait on them.
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({serving, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopper
        logger.info("shutting down")
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
        server.close()
        server.close_clients()  # Python ≥ 3.13; project requires 3.13
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving
        await server.wait_closed()
        config.socket_path.unlink(missing_ok=True)
        clear_current(config.home)
        clear_goal(config.home)
        # Last but one: an OSError from the append must not skip the cleanup
        # above it, and the lock fd stays the final statement.
        _record_session_end(hub, session_id)
        os.close(lock_fd)


def serve_main(config: Config) -> None:
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass
