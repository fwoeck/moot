"""The observer's line renderer, its stdin syntax, and hub hangup."""

import io
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from moot.core.clock import Clock
from moot.core.transcript import Transcript
from moot.spoke import observer
from moot.spoke.conn import Conn
from moot.spoke.observer import Pretty, parse_outgoing

DELIVER = {
    "t": "deliver",
    "round": 3,
    "msgs": [
        {
            "from": "beta",
            "to": "frank",
            "addressing": "direct",
            "kind": "answer",
            "text": "the   index\nis missing",
        },
        {
            "from": "system",
            "to": "alpha",
            "addressing": "direct",
            "kind": "note",
            "text": "mute is on",
        },
    ],
}


def plain(width: int | None = 120, full: bool = False) -> Pretty:
    return Pretty("frank", width, color=False, full=full)


def test_deliver_collapses_a_message_to_one_line():
    lines = plain().lines(DELIVER)
    # "the   index\nis missing" is ONE display line: newlines and runs of
    # whitespace collapse to single spaces, the round suffix ends the line
    assert lines[0].endswith("beta → you answer  the index is missing  r3")
    assert lines[1].endswith("system → alpha note  mute is on  r3")
    assert len(lines) == 2


def test_full_preserves_lfs_as_continuation_lines():
    lines = plain(full=True).lines(DELIVER)
    assert lines[0].endswith("beta → you answer  the index")
    # continuation aligns under the text column (ts is 8 chars: HH:MM:SS)
    assert lines[1] == " " * len("HH:MM:SS beta → you answer  ") + "is missing  r3"
    assert lines[2].endswith("system → alpha note  mute is on  r3")


def test_long_text_is_cut_to_the_width():
    frame = dict(DELIVER, msgs=[dict(DELIVER["msgs"][0], text="x" * 200)])
    line = plain(width=60).lines(frame)[0]
    assert line.endswith("…  r3")
    assert len(line) <= 60 + len("  r3")


def test_truncation_counts_display_cells_not_characters():
    from moot.spoke.tui import cell_width

    # ❌ is one character but two terminal cells: a char-counted cut renders
    # wider than the terminal and wraps its ellipsis onto the next row
    frame = dict(DELIVER, msgs=[dict(DELIVER["msgs"][0], text="❌ FALSE: " * 30)])
    line = plain(width=80).lines(frame)[0]
    assert line.endswith("…  r3")
    assert cell_width(line) <= 80


def test_full_keeps_the_whole_text():
    frame = dict(DELIVER, msgs=[dict(DELIVER["msgs"][0], text="x" * 200)])
    assert "x" * 200 in plain(width=60, full=True).lines(frame)[0]


def test_narrow_terminal_still_bounds_the_line():
    from moot.spoke.tui import cell_width

    # below ~53 columns the head leaves under 20 body cells; the body budget
    # is clamped to 20 so the line wraps once, not once per 20 characters
    frame = dict(DELIVER, msgs=[dict(DELIVER["msgs"][0], text="x" * 200)])
    line = plain(width=40).lines(frame)[0]
    assert line.endswith("…  r3")
    assert cell_width(line) <= len("HH:MM:SS beta → you answer  ") + 20 + len("  r3")


def test_colours_are_stable_per_sender_and_system_is_dim():
    pretty = Pretty("frank", 200, color=True, full=False)
    first = pretty.lines(DELIVER)
    second = pretty.lines(DELIVER)
    assert first[0].split("beta")[0] == second[0].split("beta")[0]
    assert "\x1b[36mbeta\x1b[0m" in first[0]
    assert "\x1b[2msystem\x1b[0m" in first[1]
    assert "\x1b[1m→ you\x1b[0m" in first[0]


def test_event_and_error_lines():
    line = plain().lines({"t": "event", "event": "stall", "detail": "quiet"})[0]
    assert line.endswith(" · stall detail=quiet")
    err = plain().lines({"t": "err", "code": "frozen", "detail": "round limit"})[0]
    assert err.endswith(" ! frozen: round limit")


def test_error_line_carries_the_retry_wait():
    frame = {
        "t": "err",
        "code": "rate_limited",
        "detail": "rate limit",
        "retry_after": 8.2,
    }
    assert (
        plain().lines(frame)[0].endswith(" ! rate_limited: rate limit (retry in 8.2s)")
    )


def test_roster_line():
    frame = {
        "t": "roster",
        "round": 7,
        "frozen": True,
        "muted": False,
        "peers": [
            {"name": "alpha", "state": "busy", "finished": True},
            {"name": "beta", "state": "idle", "finished": False},
        ],
    }
    assert (
        plain().lines(frame)[0].endswith("roster r7 frozen  alpha:busy done, beta:idle")
    )


def test_unknown_frame_renders_nothing():
    assert plain().lines({"t": "ok", "seq": 1}) == []


def test_echo_lines_render_the_observers_own_message():
    lines = plain().echo_lines("beta", "note", "my own text", ts="22:51:48")
    assert lines == ["22:51:48 frank → beta note  my own text"]


def test_echo_lines_carry_the_queued_suffix_and_collapse_lfs():
    lines = plain().echo_lines("beta", "note", "one\ntwo", queued=2, ts="22:51:48")
    assert lines == ["22:51:48 frank → beta note  one two · queued at 2 busy peer(s)"]


def test_deliver_line_carries_the_message_id():
    frame = dict(DELIVER, msgs=[dict(DELIVER["msgs"][0], id=42)])
    assert plain().lines(frame)[0].endswith("  r3 #42")


def test_deliver_line_suppresses_a_zero_id():
    # id 0 is the hub's placeholder for a dropped queue slot, not a handle
    frame = dict(DELIVER, msgs=[dict(DELIVER["msgs"][0], id=0)])
    assert plain().lines(frame)[0].endswith("  r3")


def test_deliver_line_carries_the_private_marker():
    frame = dict(DELIVER, msgs=[dict(DELIVER["msgs"][0], id=42, private=True)])
    assert plain().lines(frame)[0].endswith("  r3 #42 · private")


def test_echo_line_carries_the_private_marker():
    lines = plain().echo_lines(
        "beta", "note", "x", ts="22:51:48", msg_id=47, private=True
    )
    assert lines == ["22:51:48 frank → beta note  x  #47 · private"]


def test_echo_line_carries_the_accepted_id():
    lines = plain().echo_lines("beta", "note", "x", ts="22:51:48", msg_id=47)
    assert lines == ["22:51:48 frank → beta note  x  #47"]
    queued = plain().echo_lines("beta", "note", "x", 2, "22:51:48", 47)
    assert queued == ["22:51:48 frank → beta note  x  #47 · queued at 2 busy peer(s)"]


def test_malformed_deliver_raises():
    with pytest.raises(ValueError, match="msgs list"):
        plain().lines({"t": "deliver", "round": 1})


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("hello", {"t": "say", "to": "*", "kind": "note", "text": "hello", "seq": 1}),
        (
            "@beta hello",
            {"t": "say", "to": "beta", "kind": "note", "text": "hello", "seq": 1},
        ),
        (
            "!claim @beta it is X",
            {"t": "say", "to": "beta", "kind": "claim", "text": "it is X", "seq": 1},
        ),
        (
            "@beta !claim it is X",
            {"t": "say", "to": "beta", "kind": "claim", "text": "it is X", "seq": 1},
        ),
        (
            "!bogus text",
            {"t": "say", "to": "*", "kind": "note", "text": "text", "seq": 1},
        ),
        (
            "@beta !private hi",
            {
                "t": "say",
                "to": "beta",
                "kind": "note",
                "text": "hi",
                "seq": 1,
                "private": True,
            },
        ),
        (
            "!private @beta hi",
            {
                "t": "say",
                "to": "beta",
                "kind": "note",
                "text": "hi",
                "seq": 1,
                "private": True,
            },
        ),
        (
            "!claim !private @beta hi",
            {
                "t": "say",
                "to": "beta",
                "kind": "claim",
                "text": "hi",
                "seq": 1,
                "private": True,
            },
        ),
        (
            "!private hi",
            {
                "t": "say",
                "to": "*",
                "kind": "note",
                "text": "hi",
                "seq": 1,
                "private": True,
            },
        ),
        ("!state idle", {"t": "state", "state": "idle"}),
        (
            "!state blocked permission: bash",
            {"t": "state", "state": "blocked", "detail": "permission: bash"},
        ),
    ],
)
def test_parse_outgoing(line: str, expected: dict[str, object]):
    assert parse_outgoing(line, 1) == expected


def test_parse_outgoing_rejects_an_unknown_state():
    assert parse_outgoing("!state confused", 1) is None


def seat_with(*msgs: dict[str, object], home: Path | None = None) -> observer.Seat:
    """A seat whose ring holds `msgs`, delivered as the hub would deliver them."""
    seat = observer.Seat("frank", home or Path("/nowhere"))
    seat.observe_welcome(
        {
            "t": "welcome",
            "peers": [
                {"name": "alpha", "kind": "claude-code"},
                {"name": "beta", "kind": "opencode"},
            ],
        }
    )
    for m in msgs:
        seat.observe({"t": "deliver", "round": 1, "msgs": [m]})
    return seat


def msg(msg_id: int, text: str, sender: str = "beta", kind: str = "objection"):
    return {
        "id": msg_id,
        "from": sender,
        "to": "*",
        "addressing": "overheard",
        "kind": kind,
        "text": text,
        "ts": 1_756_000_000.0,
    }


def build(line: str, seat: observer.Seat, max_rows: int = 20) -> observer.Outgoing:
    return observer.build_outgoing(line, seat, max_rows, plain())


def write_transcript(home: Path, *records: dict[str, object]) -> None:
    """Today's transcript as the hub writes it — a real Clock, because
    FakeClock's wall time lands in 1993 and would name another file."""
    transcript = Transcript(home / "transcripts", Clock())
    for rec in records:
        transcript.append(rec)


def transcript_msg(msg_id: int, text: str, sender: str = "alpha"):
    return {
        "type": "msg",
        "id": msg_id,
        "round": 1,
        "from": sender,
        "to": "*",
        "kind": "note",
        "text": text,
        "ts": 1_756_000_000.0,
    }


def test_show_renders_a_message_from_the_ring():
    seat = seat_with(msg(41, "no, X is fine"))
    out = build("/show #41", seat)
    assert out.frames == []
    assert out.local[0].startswith("#41 ")
    assert "beta → * · objection: no, X is fine" in out.local[0]
    assert out.local[-1] == "[end of replay]"


def test_show_falls_back_to_todays_transcript(tmp_path: Path):
    write_transcript(tmp_path, transcript_msg(7, "from the file"))
    seat = seat_with(home=tmp_path)
    out = build("/show 7", seat)
    assert "alpha → * · note: from the file" in out.local[0]


def test_show_reports_an_unknown_id():
    out = build("/show #99", seat_with(msg(41, "x")))
    assert out.error == (
        "[no message #99 — not in this seat's buffer or today's transcript]"
    )
    assert out.local == []


def test_last_defaults_to_three_and_backfills_from_the_transcript(tmp_path: Path):
    write_transcript(tmp_path, transcript_msg(1, "oldest"), transcript_msg(2, "older"))
    seat = seat_with(msg(3, "newest"), home=tmp_path)
    out = build("/last", seat)
    assert [line.split(" ")[0] for line in out.local[:3]] == ["#1", "#2", "#3"]
    assert out.local[-1] == "[end of replay]"


def test_find_reports_a_bad_regex_instead_of_raising():
    out = build("/find [", seat_with(msg(1, "x")))
    assert out.error is not None
    assert out.error.startswith("[bad regex: ")


def test_find_matches_ring_texts():
    seat = seat_with(msg(1, "the index is missing"), msg(2, "unrelated"))
    out = build("/find ind[ex]+", seat)
    assert len(out.local) == 2  # one hit plus the closing marker
    assert "the index is missing" in out.local[0]
    assert build("/find zzz", seat).local == ["[no match]"]


def test_replay_is_capped_and_closed_with_a_marker():
    seat = seat_with(*(msg(i, f"line {i}") for i in range(1, 21)))
    out = build("/last 20", seat, max_rows=4)
    assert len(out.local) == 5
    assert out.local[0].startswith("#17 ")
    assert out.local[-1] == "[+16 more rows — transcript]"


def test_q_composes_a_quoting_question():
    seat = seat_with(msg(41, "x" * 600))
    out = build("/q 41 @alpha does that hold?", seat)
    assert out.frames == [
        {
            "t": "say",
            "to": "alpha",
            "kind": "question",
            "text": 'beta wrote (#41): "' + "x" * 500 + '…"\ndoes that hold?',
            "seq": 1,
        }
    ]
    assert list(out.echo) == [1]


def test_q_on_a_private_message_stays_private():
    whisper = dict(msg(41, "geheim"), to="frank", addressing="direct", private=True)
    seat = seat_with(whisper)
    out = build("/q 41 @alpha and this?", seat)
    assert out.frames[0]["private"] is True
    assert out.echo[1][3] is True


def test_q_on_a_private_message_refuses_to_broadcast():
    """The typed path refuses `@* !private` locally; the quote path must not
    reintroduce the hub round trip (`malformed`) it avoids."""
    whisper = dict(msg(41, "geheim"), to="frank", addressing="direct", private=True)
    out = build("/q 41 @* and this?", seat_with(whisper))
    assert out.frames == []
    assert out.error == "[private needs @name — a private #N cannot be broadcast]"


def test_q_on_a_public_message_carries_no_private_key():
    out = build("/q 41 @alpha and this?", seat_with(msg(41, "offen")))
    assert "private" not in out.frames[0]
    assert out.echo[1][3] is False


def test_help_is_local_only():
    out = build("/help", seat_with())
    assert out.frames == []
    assert out.local
    assert any("/show" in line for line in out.local)


def test_goal_command_carries_its_text():
    out = build("/goal find the leak", seat_with())
    assert out.frames == [
        {"t": "cmd", "cmd": "goal", "args": {"text": "find the leak"}, "seq": 1}
    ]


def test_goal_without_text_is_refused():
    seat = seat_with()
    assert build("/goal", seat).error == "[/goal needs a text]"
    assert build("/goal   ", seat).error == "[/goal needs a text]"


def test_roster_line_shows_the_goal():
    frame = {
        "t": "roster",
        "round": 7,
        "frozen": False,
        "muted": False,
        "goal": "find the leak",
        "peers": [{"name": "alpha", "state": "busy", "finished": False}],
    }
    assert plain().lines(frame)[0].endswith("alpha:busy  goal: find the leak")


def test_a_refused_line_consumes_no_seq():
    seat = seat_with()
    assert build("/show #99", seat).error is not None
    assert build("hello", seat).frames[0]["seq"] == 1


def test_seat_tracks_peers_and_the_goal_from_events():
    seat = seat_with()
    joined = {"t": "event", "event": "peer_joined", "name": "gamma", "kind": "opencode"}
    seat.observe(joined)
    assert seat.known_peer("gamma")
    seat.observe({"t": "event", "event": "peer_left", "name": "gamma"})
    assert not seat.known_peer("gamma")
    seat.observe({"t": "event", "event": "goal_set", "text": "find the leak"})
    assert seat.goal == "find the leak"
    seat.observe(
        dict(ROSTER, peers=[*ROSTER["peers"], {"name": "frank", "kind": "observer"}])
    )
    assert seat.peer_names() == ["alpha", "beta"]  # never the seat itself


def test_state_lines_pass_through_without_a_seq():
    seat = seat_with()
    assert build("!state busy", seat).frames == [{"t": "state", "state": "busy"}]
    assert build("!state bogus", seat).ignored is True


def test_replay_commands_refuse_bad_arguments():
    seat = seat_with(msg(41, "x"))
    assert build("/show later", seat).error == "[/show takes a message id]"
    assert build("/last two", seat).error == "[/last takes a count]"
    assert build("/find", seat).error == "[/find needs a regex]"
    assert build("/last", seat_with()).local == ["[nothing to replay]"]


def test_q_refuses_a_bad_target_or_a_missing_question():
    seat = seat_with(msg(41, "x"))
    assert build("/q @alpha why", seat).error == (
        "[/q takes a message id, then @name and a question]"
    )
    assert build("/q 99 @alpha why", seat).error == "[no message #99]"
    assert build("/q 41 @nobody why", seat).error == "[no peer 'nobody' — alpha, beta]"
    assert build("/q 41 @alpha", seat).error == "[/q needs a question after @name]"


def test_close_sends_a_done_then_a_reset():
    seat = seat_with()
    out = build("/close we shipped it", seat)
    assert out.frames == [
        {"t": "say", "to": "*", "kind": "done", "text": "we shipped it", "seq": 1},
        {"t": "cmd", "cmd": "reset", "seq": 2},
    ]
    assert out.echo == {1: ("*", "done", "we shipped it", False)}
    assert build("/close", seat).error == "[/close needs a closing statement]"


def test_control_characters_are_refused_with_a_column():
    out = build("hi \x01 there", seat_with())
    assert out.error == "[control characters at col 4 — not sent]"
    assert out.frames == []


def test_empty_text_after_addressing_is_refused():
    assert build("@alpha", seat_with()).error == "[empty message — not sent]"
    assert build("!claim", seat_with()).error == "[empty message — not sent]"


def test_unknown_peer_is_refused_with_the_roster():
    out = build("@bta hello", seat_with())
    assert out.error == "[no peer 'bta' — alpha, beta]"
    assert out.frames == []
    # the floor's own names always pass: broadcast and the seat itself
    assert build("hello", seat_with()).error is None
    assert build("@frank note to self", seat_with()).error is None


def test_unknown_command_is_refused_with_a_hint():
    out = build("/frezee", seat_with())
    assert out.error == (
        "[unknown command '/frezee' — /freeze /resume /reset /mute /unmute"
        " /goal /roster /show /last /find /q /close /help]"
    )


def test_known_hub_command_passes_through():
    seat = seat_with()
    assert build("/freeze", seat).frames == [{"t": "cmd", "cmd": "freeze", "seq": 1}]
    assert build("/resume 3", seat).frames == [
        {"t": "cmd", "cmd": "resume", "seq": 2, "args": {"n": 3}}
    ]


def test_private_broadcast_is_refused_locally():
    """A private say needs a named peer; the seat says so before the hub
    would answer `malformed`, and the buffer stays."""
    out = build("!private hi", seat_with())
    assert out.error == "[private needs @name]"
    assert out.frames == [] and out.echo == {}


def test_private_say_frame_and_echo():
    out = build("@alpha !private psst", seat_with())
    assert out.frames[0]["private"] is True and out.frames[0]["to"] == "alpha"
    assert list(out.echo.values()) == [("alpha", "note", "psst", True)]


def test_replay_shows_private(tmp_path: Path):
    """The marker survives both ring sources: a delivered message and a
    transcript record."""
    delivered = dict(msg(41, "psst"), to="alpha", private=True)
    out = build("/show #41", seat_with(delivered))
    assert "beta → alpha · private · objection: psst" in out.local[0]
    write_transcript(tmp_path, dict(transcript_msg(7, "from the file"), private=True))
    out = build("/show 7", seat_with(home=tmp_path))
    assert "alpha → * · private · note: from the file" in out.local[0]


def test_help_mentions_private():
    text = "\n".join(build("/help", seat_with()).local)
    assert "!private" in text
    assert "a private #N stays private" in text


def test_addressing_in_either_order():
    seat = seat_with()
    first = build("!claim @alpha it is X", seat).frames[0]
    second = build("@alpha !claim it is X", seat).frames[0]
    assert first["to"] == second["to"] == "alpha"
    assert first["kind"] == second["kind"] == "claim"
    assert first["text"] == second["text"] == "it is X"


ROSTER = {
    "t": "roster",
    "round": 7,
    "max_rounds": 24,
    "frozen": True,
    "muted": False,
    "goal": "find the leak",
    "peers": [
        {
            "name": "alpha",
            "kind": "claude-code",
            "state": "busy",
            "busy_for": 250.0,
            "finished": False,
            "queued": 2,
            "queued_from": ["frank", "beta"],
            "context": 3,
            "blocked_detail": None,
        },
        {
            "name": "beta",
            "kind": "opencode",
            "state": "idle",
            "busy_for": 0.0,
            "finished": False,
            "queued": 0,
            "queued_from": [],
            "context": 0,
            "blocked_detail": None,
        },
    ],
}


def test_status_band_shape():
    assert observer.status_band(ROSTER, 200) == (
        "r7/24 · alpha busy 4m10s 2q(frank,beta) 3c · beta idle · frozen"
        " · goal: find the leak"
    )


def test_status_band_leads_with_the_seat_name():
    """The seat's own name is the one thing the welcome line carried and the
    scrollback loses — it is what the agents address."""
    band = observer.status_band(ROSTER, 200, "frank")
    assert band.startswith("@frank · r7/24 · alpha busy")


def test_status_band_without_a_context_key():
    """A roster from a hub that does not report `context` renders no `c`."""
    peers = [{k: v for k, v in p.items() if k != "context"} for p in ROSTER["peers"]]
    band = observer.status_band(dict(ROSTER, peers=peers), 200)
    assert "3c" not in band and "0c" not in band


def test_status_band_marks_muted_and_finished_peers():
    peers = [dict(ROSTER["peers"][1], finished=True)]
    band = observer.status_band(dict(ROSTER, peers=peers, muted=True), 200)
    assert "beta idle done" in band
    assert " · frozen · muted · goal: find the leak" in band


def test_status_band_skips_observers():
    peers = [*ROSTER["peers"], {"name": "frank", "kind": "observer", "state": "idle"}]
    band = observer.status_band(dict(ROSTER, peers=peers), 200)
    # frank appears only as a queued sender, never as a floor segment
    assert "frank idle" not in band


def test_status_band_shows_blocked_detail():
    peers = [
        dict(
            ROSTER["peers"][1],
            state="blocked",
            busy_for=45.0,
            blocked_detail="permission: bash for a very long command",
        )
    ]
    band = observer.status_band(dict(ROSTER, peers=peers), 200)
    assert "beta blocked(permission: bash fo…) 45s" in band


def test_status_band_is_clipped_to_the_width():
    from moot.spoke.tui import cell_width

    band = observer.status_band(ROSTER, 30)
    assert cell_width(band) <= 28
    assert band.endswith("…")


class _FakeTty:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _fake_ttys(monkeypatch, tty: bool = True) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeTty(tty))
    monkeypatch.setattr(sys, "stdout", _FakeTty(tty))


def test_tui_unavailable_without_ttys(monkeypatch, capsys):
    _fake_ttys(monkeypatch, tty=False)
    assert observer._tui_available() is False
    assert capsys.readouterr().err == ""  # silent for pipes/scripts


def test_tui_unavailable_with_a_dumb_term(monkeypatch, capsys):
    _fake_ttys(monkeypatch)
    monkeypatch.setenv("TERM", "dumb")
    assert observer._tui_available() is False
    assert "TERM" in capsys.readouterr().err


def _terminal_size(columns: int, lines: int):
    def fake_size(fallback):
        return os.terminal_size((columns, lines))

    return fake_size


def test_tui_unavailable_below_the_minimum_size(monkeypatch, capsys):
    _fake_ttys(monkeypatch)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(observer.shutil, "get_terminal_size", _terminal_size(20, 8))
    assert observer._tui_available() is False
    assert "plain mode" in capsys.readouterr().err


def test_tui_available_on_a_proper_terminal(monkeypatch):
    _fake_ttys(monkeypatch)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(observer.shutil, "get_terminal_size", _terminal_size(120, 40))
    assert observer._tui_available() is True


class Sink(io.StringIO):
    """stdout written by both the reader thread and the seat."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            return super().write(s)

    def text(self) -> str:
        with self._lock:
            return self.getvalue()


WELCOME = {
    "t": "welcome",
    "name": "frank",
    "round": 0,
    "peers": [],
    "limits": {"rate": "6/60s", "max_rounds": 24},
}


def test_classic_mode_prints_refusals_and_local_replies(monkeypatch):
    ours, theirs = socket.socketpair()
    monkeypatch.setattr(observer, "connect", lambda *args: (Conn(ours), WELCOME))
    sink = Sink()
    read_fd, write_fd = os.pipe()
    exits: list[int] = []
    with os.fdopen(read_fd) as stdin, os.fdopen(write_fd, "wb", buffering=0) as keys:
        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(sys, "stdout", sink)
        seat = threading.Thread(
            target=lambda: exits.append(
                observer.run_observer(Path("/nowhere"), "frank", False, 100, False)
            )
        )
        seat.start()
        try:
            keys.write(b"/frezee\n/help\nhello\n")
            deadline = time.monotonic() + 5.0
            while "[unknown command" not in sink.text():
                assert time.monotonic() < deadline, sink.text()
                time.sleep(0.01)
            theirs.settimeout(5)
            assert b'"say"' in theirs.recv(4096)  # the plain line still sent
        finally:
            keys.close()  # stdin EOF: the seat says bye and returns
            seat.join(timeout=5.0)
            assert not seat.is_alive()
            ours.close()
            theirs.close()
            # let the parked reader thread report the hangup into the sink,
            # not onto the suite's real stdout after monkeypatch teardown
            deadline = time.monotonic() + 5.0
            while "[hub closed the connection]" not in sink.text():
                assert time.monotonic() < deadline, sink.text()
                time.sleep(0.01)
    assert exits == [0]
    assert "/show" in sink.text()  # /help answered locally


def test_a_typed_line_after_the_hub_hung_up_exits_instead_of_crashing(monkeypatch):
    ours, theirs = socket.socketpair()
    theirs.close()  # a hub that went away right after the welcome
    monkeypatch.setattr(observer, "connect", lambda *args: (Conn(ours), WELCOME))
    sink = Sink()
    read_fd, write_fd = os.pipe()
    exits: list[int] = []
    with os.fdopen(read_fd) as stdin, os.fdopen(write_fd, "wb", buffering=0) as keys:
        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(sys, "stdout", sink)
        seat = threading.Thread(
            target=lambda: exits.append(
                observer.run_observer(Path("/nowhere"), "frank", False, 100, False)
            )
        )
        seat.start()
        try:
            deadline = time.monotonic() + 5.0
            while "[hub closed the connection]" not in sink.text():
                assert time.monotonic() < deadline, sink.text()
                time.sleep(0.01)
            keys.write(b"anybody there?\n")
            seat.join(timeout=5.0)
        finally:
            assert not seat.is_alive()
            ours.close()
    assert exits == [1]
    assert "[hub is gone — nothing sent]" in sink.text()
