"""`moot log`: the transcript renderer, in process.

Every test injects the same `FakeClock` into the writer and into `run_log`:
`FakeClock.wall()` sits in 1993, so a real-clock reader would look for a
different day's file and render nothing.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from moot.cli import main
from moot.cli.log import run_log
from moot.core.clock import FakeClock
from moot.core.transcript import Transcript


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _at(clock: FakeClock, hhmmss: str) -> float:
    """A timestamp at `HH:MM:SS` on the clock's own day."""
    hour, minute, second = (int(part) for part in hhmmss.split(":"))
    day = datetime.fromtimestamp(clock.wall())
    return day.replace(
        hour=hour, minute=minute, second=second, microsecond=0
    ).timestamp()


def _msg(clock: FakeClock, at: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "msg",
        "id": 1,
        "round": 1,
        "from": "beta",
        "to": "*",
        "kind": "note",
        "text": "hello",
        "ts": _at(clock, at),
    }
    record.update(overrides)
    return record


def _event(clock: FakeClock, at: str, event: str, **extra: Any) -> dict[str, Any]:
    return {"type": "event", "event": event, "ts": _at(clock, at), **extra}


def _write(home: Path, clock: FakeClock, *records: dict[str, Any]) -> Transcript:
    transcript = Transcript(home / "transcripts", clock)
    for record in records:
        transcript.append(record)
    return transcript


def _run(home: Path, clock: FakeClock, **kwargs: Any) -> int:
    call: dict[str, Any] = {
        "kind": "msg",
        "since": None,
        "last": None,
        "fmt": "text",
        "out": None,
    }
    call.update(kwargs)
    return run_log(home, clock=clock, **call)


def test_text_mode_one_line_per_msg_with_its_own_ts(home, clock, capsys):
    _write(
        home,
        clock,
        _msg(clock, "12:03:41", id=7, round=2, kind="objection", text="wrong"),
        _msg(clock, "12:04:09", id=8, round=2, to="beta", text="why"),
    )

    assert _run(home, clock) == 0

    assert capsys.readouterr().out.splitlines() == [
        "12:03:41 [r2 #7] beta → * · objection: wrong",
        "12:04:09 [r2 #8] beta → beta · note: why",
    ]


@pytest.mark.parametrize("id_field", [{"id": 0}, {}])
def test_id_is_suppressed_when_zero_or_missing(home, clock, capsys, id_field):
    record = _msg(clock, "09:00:00")
    del record["id"]
    record.update(id_field)
    _write(home, clock, record)

    assert _run(home, clock) == 0

    assert capsys.readouterr().out == "09:00:00 [r1] beta → * · note: hello\n"


def test_kind_event_renders_only_events(home, clock, capsys):
    _write(
        home,
        clock,
        _msg(clock, "09:00:00"),
        _event(clock, "09:00:05", "stall", detail="all agents idle"),
    )

    assert _run(home, clock, kind="event") == 0

    assert capsys.readouterr().out == (
        "09:00:05 [event] stall · detail=all agents idle\n"
    )


def _deliver(clock: FakeClock, at: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "deliver",
        "to": "alpha",
        "round": 1,
        "msg_ids": [1],
        "ts": _at(clock, at),
    }
    record.update(overrides)
    return record


def test_kind_all_keeps_file_order_and_renders_deliver_lines(home, clock, capsys):
    """`session`/`session_end` stay hidden; a deliver record without an
    `overheard` field (written before the field existed) lists its ids
    undivided."""
    _write(
        home,
        clock,
        {"type": "session", "id": "1-2", "started": _at(clock, "08:59:00")},
        _msg(clock, "09:00:00", id=1),
        _deliver(clock, "09:00:02"),
        _event(clock, "09:00:05", "peer_left", name="alpha"),
        _msg(clock, "09:00:07", id=2, text="again"),
        {"type": "session_end", "id": "1-2", "ts": _at(clock, "09:01:00"), "round": 1},
    )

    assert _run(home, clock, kind="all") == 0

    assert capsys.readouterr().out.splitlines() == [
        "09:00:00 [r1 #1] beta → * · note: hello",
        "09:00:02 [deliver r1] → alpha · #1",
        "09:00:05 [event] peer_left · name=alpha",
        "09:00:07 [r1 #2] beta → * · note: again",
    ]


def test_kind_deliver_splits_wake_and_context(home, clock, capsys):
    """`sys` stands for an id-0 line (a placeholder or the goal); an
    observer's copy of an overheard-only frame has no wake at all."""
    _write(
        home,
        clock,
        _msg(clock, "09:00:00", id=6),
        _deliver(
            clock, "09:00:02", round=3, msg_ids=[0, 3, 4, 5, 6], overheard=[0, 3, 4, 5]
        ),
        _deliver(clock, "09:00:03", to="frank", msg_ids=[7], overheard=[7]),
        _deliver(clock, "09:00:04", to="beta", msg_ids=[8], overheard=[]),
    )

    assert _run(home, clock, kind="deliver") == 0

    assert capsys.readouterr().out.splitlines() == [
        "09:00:02 [deliver r3] → alpha · wake #6 · context sys #3 #4 #5",
        "09:00:03 [deliver r1] → frank · wake — · context #7",
        "09:00:04 [deliver r1] → beta · wake #8",
    ]


def test_kind_deliver_is_an_argparse_choice(home, clock, monkeypatch, capsys):
    _write(home, clock, _deliver(clock, "09:00:02"))
    monkeypatch.setattr("moot.cli.log.Clock", lambda: clock)

    monkeypatch.setattr(
        sys, "argv", ["moot", "log", "--home", str(home), "--kind", "deliver"]
    )
    assert main() == 0
    assert capsys.readouterr().out == "09:00:02 [deliver r1] → alpha · #1\n"

    monkeypatch.setattr(
        sys, "argv", ["moot", "log", "--home", str(home), "--kind", "nonsense"]
    )
    with pytest.raises(SystemExit) as info:
        main()
    assert info.value.code == 2


def test_since_drops_earlier_records(home, clock, capsys):
    _write(
        home,
        clock,
        _msg(clock, "09:59:59", id=1, text="before"),
        _msg(clock, "10:00:00", id=2, text="on the minute"),
        _msg(clock, "10:00:01", id=3, text="after"),
    )

    assert _run(home, clock, since="10:00") == 0

    out = capsys.readouterr().out
    assert "before" not in out
    assert [line.split(": ")[-1] for line in out.splitlines()] == [
        "on the minute",
        "after",
    ]


def test_bad_since_is_an_argparse_error(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["moot", "log", "--since", "25:99"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_last_must_be_a_positive_integer(monkeypatch, value: str):
    monkeypatch.setattr(sys, "argv", ["moot", "log", "--last", value])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2


def test_last_keeps_the_final_n(home, clock, capsys):
    _write(
        home,
        clock,
        *[_msg(clock, "09:00:00", id=n, text=f"m{n}") for n in (1, 2, 3, 4)],
    )

    assert _run(home, clock, last=2) == 0

    assert [line.split(": ")[-1] for line in capsys.readouterr().out.splitlines()] == [
        "m3",
        "m4",
    ]


def test_md_header_lists_participants_from_peer_joined(home, clock, capsys):
    _write(
        home,
        clock,
        _event(clock, "08:00:00", "peer_joined", name="alpha", kind="agent", role=""),
        _event(
            clock,
            "08:00:01",
            "peer_joined",
            name="alpha",
            kind="claude-code",
            role="prover",
        ),
        _event(
            clock, "08:00:02", "peer_joined", name="frank", kind="observer", role=""
        ),
        _msg(clock, "08:01:00", id=1, round=1),
        _msg(clock, "08:31:30", id=2, round=3),
    )

    assert _run(home, clock, fmt="md") == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("# moot session — ")
    assert lines[2] == (
        "**Participants:** alpha (claude-code, prover), frank (observer)"
    )
    # the span covers every timestamped record, the events included
    assert lines[3] == (
        "**Today:** 2 messages · 0 deliveries · 08:00:00 → 08:31:30 (31m 30s)"
    )


def test_md_body_is_id_ordered_and_verbatim(home, clock, capsys):
    _write(
        home,
        clock,
        _msg(clock, "08:02:00", id=9, round=2, kind="objection", text="second\n\tone"),
        _msg(clock, "08:01:00", id=3, round=1, to="alpha", text="first"),
    )

    assert _run(home, clock, fmt="md") == 0

    body = capsys.readouterr().out
    assert "## #3 beta → alpha · note (r1)\n\nfirst\n" in body
    assert "## #9 beta → * · objection (r2)\n\nsecond\n\tone\n" in body
    assert body.index("#3 beta") < body.index("#9 beta")


def test_md_event_appendix(home, clock, capsys):
    _write(
        home,
        clock,
        _msg(clock, "08:01:00", id=1, text="body text"),
        _event(clock, "08:01:02", "stall", detail="all agents idle"),
    )

    assert _run(home, clock, kind="all", fmt="md") == 0

    out = capsys.readouterr().out
    head, _, appendix = out.partition("## Events\n")
    assert "stall" not in head
    assert appendix.strip() == "- 08:01:02 stall · detail=all agents idle"
    assert out.count("stall") == 1


def test_torn_final_line_is_tolerated(home, clock, capsys, caplog):
    transcript = _write(home, clock, _msg(clock, "09:00:00", id=1))
    with transcript._path_for_today().open("a", encoding="utf-8") as f:
        f.write('{"type": "msg", "id": 2, "text": "half a rec')

    with caplog.at_level(logging.WARNING, logger="moot.transcript"):
        assert _run(home, clock) == 0

    assert capsys.readouterr().out == "09:00:00 [r1 #1] beta → * · note: hello\n"
    assert len(caplog.records) == 1
    assert "torn final" in caplog.records[0].getMessage()


def test_out_writes_the_file_and_prints_nothing(home, clock, capsys, tmp_path: Path):
    _write(home, clock, _msg(clock, "09:00:00", id=1))
    target = tmp_path / "session.md"

    assert _run(home, clock, out=target) == 0

    assert capsys.readouterr().out == ""
    assert target.read_text(encoding="utf-8") == (
        "09:00:00 [r1 #1] beta → * · note: hello\n"
    )


def test_missing_transcript_dir_exits_one_and_creates_nothing(home, clock, capsys):
    home.mkdir()

    assert _run(home, clock) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"moot: no transcripts at {home / 'transcripts'}" in captured.err
    assert not (home / "transcripts").exists()


def test_md_duration_spans_hours(home, clock, capsys):
    _write(
        home,
        clock,
        _msg(clock, "09:31:30", id=2, round=4),
        _msg(clock, "08:01:00", id=1, round=1),
    )

    assert _run(home, clock, fmt="md") == 0

    assert capsys.readouterr().out.splitlines()[3] == (
        "**Today:** 2 messages · 0 deliveries · 08:01:00 → 09:31:30 (1h 30m 30s)"
    )


def test_md_without_messages_names_the_participants_and_the_events_span(
    home, clock, capsys
):
    _write(
        home,
        clock,
        _event(clock, "08:00:00", "peer_joined", name="alpha", kind="agent", role=""),
        _event(clock, "08:00:30", "stall", detail="all agents idle"),
    )

    assert _run(home, clock, kind="event", fmt="md") == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[2] == "**Participants:** alpha (agent)"
    assert (
        lines[3] == "**Today:** 0 messages · 0 deliveries · 08:00:00 → 08:00:30 (30s)"
    )
    assert "## Events" in lines


def test_md_header_without_any_timestamped_record(home, clock, capsys):
    """A `session` record carries `started`, not `ts`: no span to report."""
    _write(
        home, clock, {"type": "session", "id": "1-2", "started": _at(clock, "08:00:00")}
    )

    assert _run(home, clock, fmt="md") == 0

    assert capsys.readouterr().out.splitlines()[3] == (
        "**Today:** 0 messages · 0 deliveries · —"
    )


def test_private_marker_in_text_and_md_modes(home, clock, capsys):
    """A private message is a `msg` record like any other; both renderers show
    the marker the spokes render, so the log reads the way the floor did."""
    _write(
        home,
        clock,
        _msg(clock, "08:01:00", id=3, round=1, to="alpha", text="psst", private=True),
        _msg(clock, "08:02:00", id=4, round=1, to="alpha", text="public"),
    )

    assert _run(home, clock) == 0
    text = capsys.readouterr().out
    assert "08:01:00 [r1 #3] beta → alpha · private · note: psst\n" in text
    assert "08:02:00 [r1 #4] beta → alpha · note: public\n" in text

    assert _run(home, clock, fmt="md") == 0
    body = capsys.readouterr().out
    assert "## #3 beta → alpha · private · note (r1)\n\npsst\n" in body
    assert "## #4 beta → alpha · note (r1)\n\npublic\n" in body
