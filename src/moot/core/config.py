"""Hub configuration with defaults from the feature request."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_home() -> Path:
    return Path(os.environ.get("MOOT_HOME", Path.home() / ".moot"))


@dataclass
class Config:
    home: Path = field(default_factory=_default_home)
    transcripts: Path | None = None  # transcript dir outside the (short) home

    # Wire protocol
    max_frame_bytes: int = 256 * 1024
    outbound_queue_max: int = 256  # frames buffered per connection before drop
    close_timeout: float = 2.0  # clean-close grace before transport.abort()
    drain_timeout: float = 1.0  # grace to drain an oversized frame before close

    # Floor control
    context_cap: int = 15  # max overheard messages buffered per participant
    deliver_wake_cap: int = 10  # max queued wake messages per deliver frame
    queue_cap: int = 100  # max queued wake messages held per participant
    backlog_on_join: int = 10  # transcript backlog given to late joiners
    recent_window: int = 200  # in-memory msg window feeding late-join backlog

    # Rate limit (agents only; observers exempt)
    rate_messages: int = 6
    rate_window: float = 60.0
    rate_burst: int = 3

    # Dedup window (agents only; observers exempt)
    dedup_window: float = 120.0

    # Rounds
    max_rounds: int = 24

    # State inference for participants without the "idle-events" capability
    assume_idle_after: float = 90.0

    # Watchdog
    watchdog_tick: float = 5.0
    stall_after: float = 60.0
    # an observer whose last frame is younger counts as at the seat (notifications)
    present_within: float = 60.0
    blocked_after: float = 60.0
    dead_after: float = 300.0
    dead_ttl: float = 24 * 3600.0  # dead registrations (queue + context) expire
    pong_timeout: float = 30.0
    notifications: bool = True

    @property
    def socket_path(self) -> Path:
        return self.home / "hub.sock"

    @property
    def lock_path(self) -> Path:
        return self.home / "hub.lock"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def transcript_dir(self) -> Path:
        return self.transcripts or self.home / "transcripts"
