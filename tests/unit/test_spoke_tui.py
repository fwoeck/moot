"""The TUI kit: width math, editor, history, key parser, screen choreography."""

import io
import json
import os
import threading

import pytest

from moot.spoke import tui
from moot.spoke.tui import (
    PASTE_LIMIT,
    PROMPT,
    Editor,
    History,
    KeyParser,
    Screen,
    cell_width,
    clip_cells,
    cursor_row_col,
    display_form,
    display_pos,
    wrap_rows,
)


def test_cell_width_common_cases():
    assert cell_width("abc") == 3
    assert cell_width("über") == 4  # umlauts are single-width
    assert cell_width("中文字") == 6  # CJK is double-width
    assert cell_width("éx") == 2  # combining mark adds no width
    assert cell_width("a\tb") == 3  # tab approximated as 1
    assert cell_width("❌") == 2  # emoji with East Asian Width W
    # regional indicators count 2 each: a lone one draws 2 cells, and the
    # pair's overcount (4 vs 2 drawn) truncates early instead of overflowing
    assert cell_width("🇩") == 2
    assert cell_width("🇩🇪") == 4


def test_wrap_rows_wraps_mid_word_and_keeps_lfs():
    assert wrap_rows("abcdef", 3) == ["abc", "def"]
    assert wrap_rows("ab\ncd", 10) == ["ab", "cd"]
    assert wrap_rows("ab\ncdef", 3) == ["ab", "cde", "f"]
    assert wrap_rows("", 10) == [""]
    assert wrap_rows("abc", 3) == ["abc"]  # exact fit stays on one row


def test_cursor_row_col():
    assert cursor_row_col("abcdef", 0, 3) == (0, 0)
    assert cursor_row_col("abcdef", 3, 3) == (0, 3)  # deferred wrap at margin
    assert cursor_row_col("abcdef", 4, 3) == (1, 1)
    assert cursor_row_col("ab\ncd", 3, 10) == (1, 0)
    assert cursor_row_col("ab\ncd", 5, 10) == (1, 2)


def test_history_round_trip(tmp_path):
    path = tmp_path / "h" / "observer_history"
    hist = History(path)
    assert hist.load() == []  # missing file is fine
    hist.append("first")
    hist.append("second")
    assert json.loads(path.read_text().splitlines()[0]) == "first"
    assert History(path).load() == ["first", "second"]


def test_history_skips_corrupt_lines_and_dedupes(tmp_path):
    path = tmp_path / "observer_history"
    path.write_text('{"not": "a string"}\n"nope-json\n"keep me"\n', encoding="utf-8")
    hist = History(path)
    assert hist.load() == ["keep me"]
    hist.append("keep me")  # consecutive duplicate: not stored
    hist.append("new")
    assert path.read_text().count("\n") == 4  # 3 seeded + 1 new


def test_history_trim_has_slack_and_is_atomic(tmp_path):
    path = tmp_path / "observer_history"
    path.write_text("".join(f'"entry {i}"\n' for i in range(2000)), encoding="utf-8")
    hist = History(path)
    assert len(hist.load()) == 500  # load keeps the tail
    hist.append("one more")  # 2001 entries: inside the slack, no rewrite
    assert len(path.read_text().splitlines()) == 2001
    for i in range(200):
        hist.append(f"extra {i}")  # crosses HISTORY_CAP + TRIM_SLACK
    lines = path.read_text().splitlines()
    assert len(lines) == 2000  # trimmed back to the cap
    assert json.loads(lines[-1]) == "extra 199"
    assert not (tmp_path / "observer_history.tmp").exists()


def test_history_trim_drops_corrupt_lines(tmp_path):
    path = tmp_path / "observer_history"
    good = "".join(f'"entry {i}"\n' for i in range(2201))
    path.write_text("not json\n" + good, encoding="utf-8")
    hist = History(path)
    hist.load()
    hist.append("tip")  # count is 2202 valid > cap+slack: trims
    lines = path.read_text().splitlines()
    assert len(lines) == 2000
    assert all(isinstance(json.loads(line), str) for line in lines)


def test_history_load_raises_on_undecodable_bytes(tmp_path):
    path = tmp_path / "observer_history"
    path.write_bytes(b"\xff\n")
    with pytest.raises(UnicodeDecodeError):
        History(path).load()


def test_editor_editing_and_kill_ops():
    ed = Editor()
    ed.insert("hello world")
    assert ed.buf == "hello world" and ed.pos == 11
    ed.left()
    ed.left()
    ed.insert("X")
    assert ed.buf == "hello worXld" and ed.pos == 10
    ed.backspace()
    assert ed.buf == "hello world" and ed.pos == 9
    ed.home()
    ed.delete()
    assert ed.buf == "ello world"
    ed.end()
    ed.word_left()
    assert ed.pos == 5
    ed.word_right()
    assert ed.pos == 10
    ed.word_left()
    ed.kill_word_back()
    assert ed.buf == "world" and ed.pos == 0
    ed.end()
    ed.kill_to_end()
    assert ed.buf == "world"
    ed.kill_all()
    assert ed.buf == "" and ed.pos == 0


def test_editor_newline_and_word_ops_cross_lf():
    ed = Editor()
    ed.insert("one two")
    ed.newline()
    ed.insert("three")
    assert ed.buf == "one two\nthree"
    ed.word_left()
    assert ed.pos == 8  # start of "three", across the LF


def test_editor_history_navigation_with_stash():
    ed = Editor(["first", "second"])
    ed.insert("in progress")
    ed.history_prev()
    assert ed.buf == "second"
    ed.history_prev()
    assert ed.buf == "first"
    ed.history_prev()  # at the oldest: stays
    assert ed.buf == "first"
    ed.history_next()
    assert ed.buf == "second"
    ed.history_next()  # past the newest: the stash comes back
    assert ed.buf == "in progress"
    ed.history_prev()
    ed.insert("!")  # an edit accepts the navigated line
    ed.history_next()
    assert ed.buf == "second!"


def test_editor_submit_records_and_resets():
    ed = Editor()
    ed.insert("a message")
    assert ed.submit() == "a message"
    assert ed.buf == "" and ed.pos == 0
    assert ed.history == ["a message"]
    ed.insert("a message")  # consecutive duplicate: not recorded twice
    ed.submit()
    ed.insert("   ")  # whitespace-only: not recorded
    assert ed.submit() == "   "
    assert ed.history == ["a message"]


def feed_all(parser: KeyParser, data: bytes):
    return parser.feed(data)


def test_keyparser_plain_chars_and_enter():
    p = KeyParser()
    assert p.feed(b"ab") == [("char", "a"), ("char", "b")]
    assert p.feed("\u00fc".encode()) == [("char", "\u00fc")]
    assert p.feed(b"\n") == [("enter",)]
    assert p.feed(b"\r") == [("enter",)]  # defensive: ICRNL normally maps
    assert p.feed(b"a\x7fb") == [("char", "a"), ("key", "backspace"), ("char", "b")]
    assert p.feed(b"\x08") == [("key", "backspace")]
    assert p.feed(b"\t") == [("char", "\t")]


def test_keyparser_alt_enter_and_ctrl_bytes():
    p = KeyParser()
    assert p.feed(b"\x1b\n") == [("alt_enter",)]
    assert p.feed(b"\x1b\r") == [("alt_enter",)]
    keys = p.feed(bytes([0x01, 0x03, 0x04, 0x05, 0x0B, 0x0C, 0x15, 0x17]))
    names = [k[1] for k in keys]
    assert names == [
        "ctrl_a",
        "ctrl_c",
        "ctrl_d",
        "ctrl_e",
        "ctrl_k",
        "ctrl_l",
        "ctrl_u",
        "ctrl_w",
    ]


def test_keyparser_csi_and_ss3_navigation():
    p = KeyParser()
    assert p.feed(b"\x1b[A\x1b[B\x1b[C\x1b[D") == [
        ("key", "up"),
        ("key", "down"),
        ("key", "right"),
        ("key", "left"),
    ]
    assert p.feed(b"\x1b[H\x1b[F") == [("key", "home"), ("key", "end")]
    assert p.feed(b"\x1b[3~") == [("key", "delete")]
    assert p.feed(b"\x1b[1~\x1b[4~\x1b[7~\x1b[8~") == [
        ("key", "home"),
        ("key", "end"),
        ("key", "home"),
        ("key", "end"),
    ]
    # SS3 forms
    assert p.feed(b"\x1bOH\x1bOF\x1bOA") == [
        ("key", "home"),
        ("key", "end"),
        ("key", "up"),
    ]
    # shift-tab and unknown sequences are ignored
    assert p.feed(b"\x1b[Z\x1b[9~") == []


def test_keyparser_alt_word_keys():
    p = KeyParser()
    assert p.feed(b"\x1bb\x1bf") == [("key", "alt_b"), ("key", "alt_f")]


def test_keyparser_split_sequence_and_bare_escape_timeout():
    p = KeyParser()
    assert p.feed(b"\x1b") == []
    assert p.pending_escape
    assert p.feed(b"[A") == [("key", "up")]
    assert not p.pending_escape
    p.feed(b"\x1b")
    assert p.flush_timeout() == []  # a bare Escape is discarded
    assert not p.pending_escape
    assert p.feed(b"x") == [("char", "x")]


def test_display_form_makes_control_bytes_visible():
    assert display_form("p\x1b[2Jq") == "p^[[2Jq"
    assert display_form("a\tb") == "a^Ib"
    assert display_form("x\x7f") == "x^?"
    assert display_form("c\x85d") == "c�d"
    assert display_form("one\ntwo") == "one\ntwo"  # LF stays a line break
    assert display_pos("a\tb", 2) == 3  # cursor after the 2-cell ^I


def test_keyparser_unterminated_paste_ends_on_timeout():
    p = KeyParser()
    assert p.feed(b"\x1b[200~abc") == []
    assert p.pending_paste
    assert p.flush_timeout() == [("paste", "abc")]
    assert not p.pending_paste
    assert p.feed(b"x") == [("char", "x")]


def test_keyparser_paste_is_capped():
    p = KeyParser()
    keys = p.feed(b"\x1b[200~" + b"y" * (PASTE_LIMIT + 8))
    assert keys and keys[0][0] == "paste"
    assert len(keys[0][1]) >= PASTE_LIMIT
    assert not p.pending_paste


def test_keyparser_replaces_invalid_utf8():
    p = KeyParser()
    assert p.feed(b"a\xffb") == [
        ("char", "a"),
        ("char", "�"),
        ("char", "b"),
    ]


def test_keyparser_bracketed_paste():
    p = KeyParser()
    assert p.feed(b"\x1b[200~line1\r\nline2\x1b[201~") == [("paste", "line1\nline2")]
    assert not p.pending_escape
    # paste split across feeds, with bytes after the terminator
    assert p.feed(b"\x1b[200~pa") == []
    assert p.feed(b"ste\x1b[201~tail") == [
        ("paste", "paste"),
        ("char", "t"),
        ("char", "a"),
        ("char", "i"),
        ("char", "l"),
    ]


class FakeScreen:
    """A Screen writing to a StringIO, with an injected terminal size."""

    def __init__(self, cols: int = 80, rows: int = 24, color: bool = False):
        self.out = io.StringIO()
        self.screen = Screen(out=self.out, size=(cols, rows), color=color)

    def text(self) -> str:
        return self.out.getvalue()


def test_screen_setup_and_teardown_sequences():
    fake = FakeScreen()
    fake.screen.setup()
    out = fake.text()
    assert "\x1b[2J\x1b[H" in out
    assert "\x1b[1;22r" in out  # region: 24 rows - 2 footer rows
    assert "\x1b[?2004h" in out
    assert Screen.DIVIDER_HINT in out
    fake.screen.teardown()
    out = fake.text()
    assert "\x1b[r\x1b[?2004l" in out
    assert "\x1b[24;1H\n" in out


def test_screen_print_lines_moves_cursor_into_region_and_back():
    fake = FakeScreen()
    fake.screen.setup()
    fake.screen.redraw_footer(Editor())
    fake.text()  # baseline
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.print_lines(["first message", "second message"])
    out = fake.text()
    assert "\x1b[22;1H" in out  # cup to region bottom
    # each line clears its row first: a resize can leave stale content there
    assert "\x1b[2Kfirst message\n\x1b[2Ksecond message\n" in out
    assert out.endswith("\x1b[24;3H")  # cursor back into the input, after the prompt


def test_screen_grow_scrolls_region_before_shrinking_it():
    fake = FakeScreen()
    fake.screen.setup()
    ed = Editor()
    fake.screen.redraw_footer(ed)
    fake.out.seek(0)
    fake.out.truncate(0)
    ed.insert("x" * 200)  # wraps to 3 rows at width 80: footer 2 -> 4
    fake.screen.redraw_footer(ed)
    out = fake.text()
    assert fake.screen.footer_rows == 4
    scroll_up = out.index("\x1b[22;1H\n\n")  # scroll region up by delta first
    region_reset = out.index("\x1b[1;20r")
    assert scroll_up < region_reset  # preservation scroll comes before DECSTBM


def test_screen_shrink_sets_region_then_erases_stale_rows():
    fake = FakeScreen()
    fake.screen.setup()
    ed = Editor()
    ed.insert("x" * 200)
    fake.screen.redraw_footer(ed)
    assert fake.screen.footer_rows == 4
    fake.out.seek(0)
    fake.out.truncate(0)
    ed.kill_all()
    fake.screen.redraw_footer(ed)
    out = fake.text()
    assert fake.screen.footer_rows == 2
    region_reset = out.index("\x1b[1;22r")
    stale_erase = out.index("\x1b[21;1H\x1b[J")  # old divider row, now stale
    assert region_reset < stale_erase


def test_screen_height_is_capped_and_window_follows_cursor():
    fake = FakeScreen()
    fake.screen.setup()
    ed = Editor()
    ed.insert("x" * 1000)  # 13 visual rows at width 80
    fake.screen.redraw_footer(ed)
    assert fake.screen.footer_rows == 1 + 5  # capped at MAX_INPUT_ROWS
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.redraw_footer(ed)  # cursor at the end: window shows the tail
    assert PROMPT not in fake.text()  # the prompt row is scrolled out of view
    ed.home()  # cursor to the top: the visible window follows
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.redraw_footer(ed)
    out = fake.text()
    assert PROMPT + "x" in out  # the first visual row is drawn again
    assert fake.screen._window_top == 0


def test_screen_window_top_clamps_when_the_input_shrinks():
    fake = FakeScreen()
    fake.screen.setup()
    ed = Editor()
    ed.insert("x" * 1000)  # 13 visual rows at width 80, window top = 8
    fake.screen.redraw_footer(ed)
    assert fake.screen._window_top == 8
    for _ in range(700):
        ed.backspace()  # back to 300 chars = 4 visual rows
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.redraw_footer(ed)
    assert fake.screen._window_top == 0  # clamped: the prompt row is back
    assert PROMPT + "x" in fake.text()


def test_screen_shrink_scrolls_the_region_back_down():
    fake = FakeScreen()
    fake.screen.setup()
    ed = Editor()
    ed.insert("x" * 200)  # footer 2 -> 4
    fake.screen.redraw_footer(ed)
    fake.out.seek(0)
    fake.out.truncate(0)
    ed.kill_all()  # footer 4 -> 2
    fake.screen.redraw_footer(ed)
    out = fake.text()
    region_reset = out.index("\x1b[1;22r")
    stale_erase = out.index("\x1b[21;1H\x1b[J")
    scroll_down = out.index("\x1b[2T")  # the released rows come back
    assert region_reset < stale_erase < scroll_down


def test_screen_divider_row_stays_valid_below_footer_height():
    fake = FakeScreen(cols=80, rows=4)
    fake.screen.setup()
    ed = Editor()
    ed.insert("x" * 400)  # forces footer_rows=6 > rows
    fake.screen.redraw_footer(ed)
    assert fake.screen.region_bottom == 1
    assert fake.screen.divider_row == 2
    assert "[-" not in fake.text()  # no negative CUP rows emitted


def test_screen_query_size_prefers_the_tty_fd(monkeypatch):
    monkeypatch.setenv("COLUMNS", "33")
    monkeypatch.setenv("LINES", "11")
    monkeypatch.setattr(
        tui.os, "get_terminal_size", lambda fd: os.terminal_size((91, 31))
    )
    screen = Screen(fd=0, out=io.StringIO())
    assert (screen.cols, screen.rows) == (91, 31)


def test_footer_never_emits_buffer_control_bytes():
    fake = FakeScreen()
    fake.screen.setup()
    ed = Editor()
    ed.insert("p\x1b[2Jq")
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.redraw_footer(ed)
    out = fake.text()
    assert "p^[[2Jq" in out
    assert "\x1b[2J" not in out  # the pasted ESC is displayed, not executed


def test_screen_resize_resets_region_and_caller_redraws():
    fake = FakeScreen()
    fake.screen.setup()
    ed = Editor()
    ed.insert("hello")
    fake.screen.redraw_footer(ed)
    fake.screen.resize((100, 30))
    assert "\x1b[1;28r" in fake.text()
    assert fake.screen.cols == 100 and fake.screen.rows == 30
    fake.screen.redraw_footer(ed)  # caller's part: no exception, sane layout
    assert "hello" in fake.text()


def test_screen_resize_erases_the_footer_of_the_old_size():
    fake = FakeScreen()  # 80x24: divider row 23
    fake.screen.setup()
    ed = Editor()
    fake.screen.redraw_footer(ed)
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.resize((80, 30))  # one border-drag step
    out = fake.text()
    assert out.index("\x1b[1;28r") < out.index("\x1b[23;1H\x1b[J")  # old footer gone
    fake.screen.redraw_footer(ed)
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.resize((80, 34))  # the next drag step erases the 30-row footer
    assert "\x1b[29;1H\x1b[J" in fake.text()


def test_clip_cells_counts_display_cells():
    assert clip_cells("abcdef", 10) == "abcdef"
    assert clip_cells("abcdef", 4) == "abc…"
    # ❌ is one character but two cells: a char-counted cut overflows the row
    clipped = clip_cells("❌" * 10, 5)
    assert cell_width(clipped) <= 5
    assert clip_cells("abc", 0) == ""


def test_set_status_replaces_the_hint_in_the_divider():
    fake = FakeScreen()
    fake.screen.setup()
    assert Screen.DIVIDER_HINT in fake.text()  # nothing pre-set at setup
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.set_status("r7/24 · alpha busy")
    out = fake.text()
    assert "r7/24 · alpha busy" in out
    assert Screen.DIVIDER_HINT not in out


def test_set_status_does_not_erase_the_input_rows():
    fake = FakeScreen()
    fake.screen.setup()
    fake.screen.redraw_footer(Editor())
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.set_status("r1 · alpha idle")
    out = fake.text()
    assert out.startswith("\x1b[23;1H\x1b[2K")  # the divider row, cleared
    assert "\x1b[J" not in out  # would erase the input rows below it
    assert out.endswith("\x1b[24;3H")  # cursor back into the input


def test_status_survives_a_resize():
    fake = FakeScreen()
    fake.screen.setup()
    fake.screen.set_status("r7/24 · alpha busy")
    fake.screen.resize((100, 30))
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.redraw_footer(Editor())
    assert "r7/24 · alpha busy" in fake.text()


def test_long_status_is_clipped():
    fake = FakeScreen(cols=40)
    fake.screen.setup()
    fake.out.seek(0)
    fake.out.truncate(0)
    fake.screen.set_status("r7/24 · " + "alpha busy 9m59s · " * 10)
    row = fake.text().split("\x1b[2K")[1].split("\x1b")[0]
    assert cell_width(row) <= 40


def test_screen_lock_serializes_concurrent_writers():
    fake = FakeScreen()
    fake.screen.setup()
    ed = Editor()

    def writer():
        for i in range(50):
            fake.screen.print_lines([f"line {i}"])

    def redrawer():
        for _ in range(50):
            fake.screen.redraw_footer(ed)

    def statuser():
        for i in range(50):
            fake.screen.set_status(f"r{i} · alpha idle")

    threads = [
        threading.Thread(target=f)
        for _ in range(2)
        for f in (writer, redrawer, statuser)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    # no torn output: every printed line is intact somewhere in the stream
    for i in range(50):
        assert f"line {i}\n" in fake.text()
