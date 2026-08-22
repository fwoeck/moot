"""macOS notifications for watchdog events. Injectable for tests."""

import asyncio


class Notifier:
    async def notify(self, title: str, message: str) -> None:  # pragma: no cover
        raise NotImplementedError


class NullNotifier(Notifier):
    async def notify(self, title: str, message: str) -> None:
        pass


class CollectingNotifier(Notifier):
    """Test double: records notifications."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def notify(self, title: str, message: str) -> None:
        self.calls.append((title, message))


class OsascriptNotifier(Notifier):
    _SCRIPT = (
        "on run argv\n"
        "display notification (item 1 of argv) with title (item 2 of argv)\n"
        "end run"
    )

    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout

    async def notify(self, title: str, message: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                self._SCRIPT,
                "--",
                message,
                title,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return  # notification is best-effort, never hub-fatal
        try:
            async with asyncio.timeout(self._timeout):
                await proc.wait()
        except TimeoutError:
            proc.kill()  # a hung osascript must not hold the hub lock (P6.3)
