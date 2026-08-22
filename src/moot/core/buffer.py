"""Per-participant context buffer with FIFO eviction (see docs/PROTOCOL.md
"Floor control").

Holds overheard messages. Drained only together with at least one wake
message — never delivered alone. On eviction a placeholder line rides along
on the next drain, carrying the count of dropped messages.
"""

from collections import deque

from moot.core import proto
from moot.core.types import Msg


class ContextBuffer:
    def __init__(self, cap: int) -> None:
        if cap <= 0:
            raise ValueError("cap must be positive")
        self.cap = cap
        self._items: deque[Msg] = deque()
        self._evicted = 0

    def append(self, msg: Msg) -> None:
        while len(self._items) >= self.cap:
            self._items.popleft()
            self._evicted += 1
        self._items.append(msg)

    def _placeholder(self) -> Msg:
        return Msg(
            id=0,
            sender=proto.SYSTEM_SENDER,
            to="*",
            kind="note",
            text=f"{self._evicted} older messages omitted",
            ts=0.0,
            addressing="overheard",
        )

    def drain(self, upto_id: int | None = None) -> list[Msg]:
        """Drain buffered messages (all, or only those with id <= upto_id),
        preceded by the eviction placeholder if any eviction happened."""
        out: list[Msg] = []
        if self._evicted:
            out.append(self._placeholder())
            self._evicted = 0
        while self._items and (upto_id is None or self._items[0].id <= upto_id):
            out.append(self._items.popleft())
        return out

    def items(self) -> tuple[Msg, ...]:
        """Read-only view (used by invariant checks)."""
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)
