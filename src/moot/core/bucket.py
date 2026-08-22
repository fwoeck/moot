"""Token bucket rate limiter for the per-agent say rate."""

from moot.core.clock import Clock


class TokenBucket:
    def __init__(self, clock: Clock, capacity: int, refill_per_second: float) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("capacity and refill rate must be positive")
        self._clock = clock
        self.capacity = float(capacity)
        self.refill_per_second = refill_per_second
        self._tokens = float(capacity)
        self._last = clock.monotonic()

    def _refill(self) -> None:
        now = self._clock.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(
                self.capacity, self._tokens + elapsed * self.refill_per_second
            )
            self._last = now

    def take(self) -> bool:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def retry_after(self) -> float:
        """Seconds until the next token is available (0 if one is available)."""
        self._refill()
        missing = 1.0 - self._tokens
        if missing <= 0:
            return 0.0
        return missing / self.refill_per_second
