"""Per-sender duplicate detection over a sliding time window."""

import hashlib

from moot.core.clock import Clock


class DedupWindow:
    def __init__(self, clock: Clock, window: float) -> None:
        self._clock = clock
        self.window = window
        self._seen: dict[str, float] = {}

    def _key(self, to: str, text: str, private: bool) -> str:
        return hashlib.sha256(f"{to}\n{int(private)}\n{text}".encode()).hexdigest()

    def _expire(self) -> None:
        cutoff = self._clock.monotonic() - self.window
        self._seen = {k: ts for k, ts in self._seen.items() if ts >= cutoff}

    def seen(self, to: str, text: str, private: bool = False) -> bool:
        """True if this sender sent an identical (to, private, text) within
        the window.

        Records the message on first sight; repeats within the window report
        True. Distinct recipients never collide, and the same words said
        publicly and then privately are two messages.
        """
        self._expire()
        key = self._key(to, text, private)
        if key in self._seen:
            return True
        self._seen[key] = self._clock.monotonic()
        return False
