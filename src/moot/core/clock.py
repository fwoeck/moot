"""Injectable time source. Tests drive a FakeClock; production uses Clock."""

import time


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class FakeClock(Clock):
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        # Fixed offset so wall timestamps look like real epoch seconds.
        return self.now + 750_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds
