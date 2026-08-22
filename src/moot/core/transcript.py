"""Append-only JSONL transcript and restart rebuild.

One file per day. Record types:
  msg     — every accepted say (round is the counter at send time)
  deliver — every deliver frame, with recipient and message ids
  event   — hub events (stall, round_limit, ...)

The transcript is the only reliable history; on hub restart the recent-msg
window is reseeded from yesterday's and today's files so late-join backlog
and context-buffer reconstruction still work across a midnight restart
(see docs/OPERATIONS.md "Transcripts").
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from moot.core.clock import Clock

logger = logging.getLogger("moot.transcript")


class Transcript:
    def __init__(self, directory: Path, clock: Clock) -> None:
        self._dir = directory
        self._clock = clock
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, day_offset: int) -> Path:
        day = datetime.fromtimestamp(self._clock.wall()) - timedelta(days=day_offset)
        return self._dir / f"{day.strftime('%Y-%m-%d')}.jsonl"

    def _path_for_today(self) -> Path:
        return self._path_for(0)

    def append(self, record: dict[str, Any]) -> None:
        with self._path_for_today().open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_day(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as f:
            lines = f.read().split("\n")
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                rest = lines[lineno:]  # lines after this one
                is_last = all(not r.strip() for r in rest)
                logger.warning(
                    "%s:%d: %s JSON record — %s",
                    path,
                    lineno,
                    "torn final" if is_last else "corrupt",
                    "ignored",
                )
        return records

    def read_today(self) -> list[dict[str, Any]]:
        return self.read_day(self._path_for_today())

    def recent_msgs(self) -> list[dict[str, Any]]:
        """msg records from yesterday and today, oldest first."""
        records = self.read_day(self._path_for(1)) + self.read_day(self._path_for(0))
        return [r for r in records if r.get("type") == "msg"]
