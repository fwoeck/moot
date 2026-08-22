"""Unit tests for the JSONL transcript: directory creation, tolerance for
torn/corrupt lines (loud, never silent), and the two-day reseed window."""

import logging
from pathlib import Path

from moot.core.clock import FakeClock
from moot.core.transcript import Transcript


def test_directory_is_created_by_the_constructor(tmp_path: Path):
    directory = tmp_path / "nested" / "transcripts"
    Transcript(directory, FakeClock())
    assert directory.is_dir()


def test_torn_final_line_is_skipped_with_a_warning(tmp_path: Path, caplog):
    t = Transcript(tmp_path, FakeClock())
    t.append({"type": "msg", "id": 1})
    t.append({"type": "msg", "id": 2})
    with t._path_for_today().open("a", encoding="utf-8") as f:
        f.write('{"type": "msg", "id": 3, "text": "half a rec')  # crash mid-write

    with caplog.at_level(logging.WARNING, logger="moot.transcript"):
        records = t.read_today()

    assert [r["id"] for r in records] == [1, 2]
    assert len(caplog.records) == 1
    assert "torn final" in caplog.records[0].getMessage()


def test_corrupt_middle_line_is_skipped_with_a_warning(tmp_path: Path, caplog):
    t = Transcript(tmp_path, FakeClock())
    t.append({"type": "msg", "id": 1})
    with t._path_for_today().open("a", encoding="utf-8") as f:
        f.write("}not json{\n")
    t.append({"type": "msg", "id": 3})

    with caplog.at_level(logging.WARNING, logger="moot.transcript"):
        records = t.read_today()

    assert [r["id"] for r in records] == [1, 3]
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "corrupt" in message and "torn" not in message


def test_read_day_of_a_missing_file_is_empty(tmp_path: Path):
    t = Transcript(tmp_path, FakeClock())
    assert t.read_day(tmp_path / "1999-01-01.jsonl") == []


def test_recent_msgs_spans_yesterday_and_today(tmp_path: Path):
    clock = FakeClock()
    t = Transcript(tmp_path, clock)
    t.append({"type": "msg", "id": 1})
    t.append({"type": "event", "event": "stall"})
    clock.advance(86_400)
    t.append({"type": "msg", "id": 2})

    assert t._path_for(1) != t._path_for(0)  # the day really rolled over
    assert [r["id"] for r in t.recent_msgs()] == [1, 2]
    assert [r["id"] for r in t.read_today()] == [2]
