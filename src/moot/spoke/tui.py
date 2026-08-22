"""Terminal kit for the observer seat's TUI.

No moot protocol knowledge in here: display-width math, a line editor, a
JSONL history, an incremental key parser, and a `Screen` that keeps a
scrolling message region (DECSTBM) above a sticky input footer. Everything
but `Screen`'s terminal I/O is pure and unit-tested directly.
"""

import codecs
import json
import os
import shutil
import sys
import termios
import threading
import tty
import unicodedata
from pathlib import Path
from typing import TextIO

PROMPT = "› "  # noqa: RUF001 — a deliberate prompt glyph, not a typo for `>`
MAX_INPUT_ROWS = 5
MIN_COLS = 40
MIN_ROWS = 10
PASTE_LIMIT = 256 * 1024  # a longer paste is treated as terminated
HISTORY_KEEP = 500
HISTORY_CAP = 2000
TRIM_SLACK = 200  # rewrite the file every ~200 appends, not on every one


def cell_width(text: str) -> int:
    """Approximate display width: W/F and regional indicators → 2,
    combining → 0, tab → 1, else 1.

    No wcwidth in the stdlib; emoji ZWJ sequences, skin tones and flag pairs
    overcount (truncating early — never overflowing), ASCII/Latin/CJK are
    exact.
    """
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        o = ord(ch)
        wide = 0x1F1E6 <= o <= 0x1F1FF or unicodedata.east_asian_width(ch) in (
            "W",
            "F",
        )
        width += 2 if wide else 1
    return width


def wrap_rows(text: str, width: int) -> list[str]:
    """Char-wrap `text` (which may contain LFs) into rows of at most `width`
    cells. Always at least one row."""
    rows: list[str] = []
    row: list[str] = []
    row_width = 0
    for ch in text:
        if ch == "\n":
            rows.append("".join(row))
            row, row_width = [], 0
            continue
        w = cell_width(ch)
        if row_width + w > width:
            rows.append("".join(row))
            row, row_width = [], 0
        row.append(ch)
        row_width += w
    rows.append("".join(row))
    return rows


def clip_cells(text: str, cells: int) -> str:
    """`text` cut to at most `cells` display cells, the ellipsis included."""
    if cell_width(text) <= cells:
        return text
    if cells <= 0:
        return ""
    kept: list[str] = []
    used = 0
    for ch in text:
        w = cell_width(ch)
        if used + w > cells - 1:
            break
        kept.append(ch)
        used += w
    return "".join(kept) + "…"


def cursor_row_col(text: str, pos: int, width: int) -> tuple[int, int]:
    """Visual (row, col) of character position `pos` in `text`, wrapped like
    `wrap_rows`. 0-based; col may equal `width` (terminal defers the wrap)."""
    row = 0
    col = 0
    for ch in text[:pos]:
        if ch == "\n":
            row += 1
            col = 0
            continue
        w = cell_width(ch)
        if col + w > width:
            row += 1
            col = 0
        col += w
    return row, col


def display_form(text: str) -> str:
    """The buffer as drawn: C0 controls (except LF) and DEL become caret
    notation, C1 becomes U+FFFD. The terminal must never execute buffer
    bytes, and the width math must measure exactly what is drawn."""
    out: list[str] = []
    for ch in text:
        o = ord(ch)
        if ch == "\n":
            out.append(ch)
        elif o < 0x20:
            out.append("^" + chr(o + 0x40))
        elif o == 0x7F:
            out.append("^?")
        elif 0x80 <= o <= 0x9F:
            out.append("�")
        else:
            out.append(ch)
    return "".join(out)


def display_pos(text: str, pos: int) -> int:
    """Cursor position within `display_form(text)` for `pos` within `text`."""
    return len(display_form(text[:pos]))


class History:
    """The global observer history: one JSON string per line (JSONL, like the
    transcripts). I/O errors propagate; the seat decides how to degrade."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._loaded = False
        self._last: str | None = None
        self._count = 0

    def load(self) -> list[str]:
        entries: list[str] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a corrupt line is skipped, never fatal
            if isinstance(entry, str):
                entries.append(entry)
        self._loaded = True
        self._count = len(entries)
        self._last = entries[-1] if entries else None
        return entries[-HISTORY_KEEP:]

    def append(self, entry: str) -> None:
        if not self._loaded:
            self.load()
        if entry == self._last:
            return  # consecutive duplicates are not stored twice
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._last = entry
        self._count += 1
        if self._count > HISTORY_CAP + TRIM_SLACK:
            self._trim()

    def _trim(self) -> None:
        """Atomically rewrite the file with the last HISTORY_CAP parseable
        entries (corrupt lines are dropped for the count and the content)."""
        kept: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, str):
                kept.append(line)
        kept = kept[-HISTORY_CAP:]
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
        tmp.replace(self.path)
        self._count = len(kept)


class Editor:
    """The composed message: buffer, cursor, kill ops, history navigation.
    Pure logic — the caller persists submissions via `History.append`."""

    def __init__(self, history: list[str] | None = None) -> None:
        self.buf = ""
        self.pos = 0
        self.history = list(history or [])
        self._hidx: int | None = None
        self._stash = ""

    def _touch(self) -> None:
        self._hidx = None  # any edit accepts the line being navigated to

    def insert(self, text: str) -> None:
        self._touch()
        self.buf = self.buf[: self.pos] + text + self.buf[self.pos :]
        self.pos += len(text)

    def newline(self) -> None:
        self.insert("\n")

    def backspace(self) -> None:
        if self.pos > 0:
            self._touch()
            self.buf = self.buf[: self.pos - 1] + self.buf[self.pos :]
            self.pos -= 1

    def delete(self) -> None:
        if self.pos < len(self.buf):
            self._touch()
            self.buf = self.buf[: self.pos] + self.buf[self.pos + 1 :]

    def left(self) -> None:
        self.pos = max(0, self.pos - 1)

    def right(self) -> None:
        self.pos = min(len(self.buf), self.pos + 1)

    def home(self) -> None:
        self.pos = 0

    def end(self) -> None:
        self.pos = len(self.buf)

    def word_left(self) -> None:
        pos = self.pos
        while pos > 0 and self.buf[pos - 1].isspace():
            pos -= 1
        while pos > 0 and not self.buf[pos - 1].isspace():
            pos -= 1
        self.pos = pos

    def word_right(self) -> None:
        pos = self.pos
        n = len(self.buf)
        while pos < n and self.buf[pos].isspace():
            pos += 1
        while pos < n and not self.buf[pos].isspace():
            pos += 1
        self.pos = pos

    def kill_word_back(self) -> None:
        end = self.pos
        self.word_left()
        if self.pos != end:
            self._touch()
            self.buf = self.buf[: self.pos] + self.buf[end:]

    def kill_to_end(self) -> None:
        if self.pos < len(self.buf):
            self._touch()
            self.buf = self.buf[: self.pos]

    def kill_all(self) -> None:
        if self.buf:
            self._touch()
            self.buf = ""
            self.pos = 0

    clear = kill_all

    def history_prev(self) -> None:
        if not self.history:
            return
        if self._hidx is None:
            self._stash = self.buf
            self._hidx = len(self.history) - 1
        elif self._hidx > 0:
            self._hidx -= 1
        else:
            return  # already at the oldest entry
        self.buf = self.history[self._hidx]
        self.pos = len(self.buf)

    def history_next(self) -> None:
        if self._hidx is None:
            return
        if self._hidx < len(self.history) - 1:
            self._hidx += 1
            self.buf = self.history[self._hidx]
        else:
            self._hidx = None
            self.buf = self._stash
        self.pos = len(self.buf)

    def submit(self) -> str:
        """Return the composed text, record it in the in-memory history, and
        reset the buffer for the next message."""
        text = self.buf
        if text.strip() and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.buf = ""
        self.pos = 0
        self._hidx = None
        self._stash = ""
        return text


# A Key is one of: ("char", str) | ("paste", str) | ("enter",) |
# ("alt_enter",) | ("key", name) with name in KEY_NAMES.
Key = tuple[str, ...]

KEY_NAMES = (
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "delete",
    "backspace",
    "ctrl_a",
    "ctrl_c",
    "ctrl_d",
    "ctrl_e",
    "ctrl_k",
    "ctrl_l",
    "ctrl_u",
    "ctrl_w",
    "alt_b",
    "alt_f",
)

_CTRL_KEYS = {
    0x01: "ctrl_a",
    0x03: "ctrl_c",
    0x04: "ctrl_d",
    0x05: "ctrl_e",
    0x0B: "ctrl_k",
    0x0C: "ctrl_l",
    0x15: "ctrl_u",
    0x17: "ctrl_w",
}

_CSI_FINALS = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
}

_CSI_TILDE = {"1": "home", "3": "delete", "4": "end", "7": "home", "8": "end"}

_PASTE_END = b"\x1b[201~"


class KeyParser:
    """Incremental byte → key parser: plain UTF-8, CSI/SS3 escape sequences,
    Alt-Enter (`ESC`+CR/LF), and bracketed paste (200~…201~)."""

    def __init__(self) -> None:
        self._state = "GROUND"
        self._csi = ""
        self._paste = bytearray()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    @property
    def pending_escape(self) -> bool:
        """Mid escape sequence — the read loop should use a short timeout so
        a bare Escape key can be discarded via `flush_timeout`."""
        return self._state in ("ESC", "CSI", "SS3")

    @property
    def pending_paste(self) -> bool:
        """Mid-paste — the read loop should use a generous timeout so a paste
        whose closing marker never arrives degrades into a plain insert."""
        return self._state == "PASTE"

    def feed(self, data: bytes) -> list[Key]:
        keys: list[Key] = []
        i = 0
        while i < len(data):
            b = data[i]
            i += 1
            if self._state == "PASTE":
                self._paste.append(b)
                if self._paste.endswith(_PASTE_END):
                    keys.append(self._end_paste(self._paste[: -len(_PASTE_END)]))
                elif len(self._paste) >= PASTE_LIMIT:
                    keys.append(self._end_paste(self._paste))
                continue
            if self._state == "ESC":
                self._state = "GROUND"
                if b == 0x5B:  # [
                    self._state = "CSI"
                    self._csi = ""
                elif b == 0x4F:  # O
                    self._state = "SS3"
                elif b in (0x0A, 0x0D):
                    keys.append(("alt_enter",))
                elif b == 0x62:  # b
                    keys.append(("key", "alt_b"))
                elif b == 0x66:  # f
                    keys.append(("key", "alt_f"))
                # anything else after a bare ESC is swallowed with it
                continue
            if self._state == "SS3":
                self._state = "GROUND"
                name = _CSI_FINALS.get(chr(b))
                if name:
                    keys.append(("key", name))
                continue
            if self._state == "CSI":
                ch = chr(b)
                if 0x40 <= b <= 0x7E:  # final byte
                    self._state = "GROUND"
                    if ch == "~":
                        if self._csi == "200":
                            self._state = "PASTE"
                            self._paste = bytearray()
                        else:
                            name = _CSI_TILDE.get(self._csi)
                            if name:
                                keys.append(("key", name))
                    elif ch != "Z":  # shift-tab: ignored
                        name = _CSI_FINALS.get(ch)
                        if name:
                            keys.append(("key", name))
                    self._csi = ""
                else:
                    self._csi += ch
                continue
            # GROUND
            if b == 0x1B:
                self._state = "ESC"
            elif b in (0x0A, 0x0D):
                keys.append(("enter",))
            elif b in (0x7F, 0x08):
                keys.append(("key", "backspace"))
            elif b == 0x09:
                keys.append(("char", "\t"))
            elif b in _CTRL_KEYS:
                keys.append(("key", _CTRL_KEYS[b]))
            elif b < 0x20:
                continue  # other control bytes: ignored
            elif b < 0x80:
                keys.append(("char", chr(b)))
            else:
                out = self._decoder.decode(bytes([b]))
                if out:
                    keys.append(("char", out))
        return keys

    def _end_paste(self, raw: bytes | bytearray) -> Key:
        text = raw.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._paste = bytearray()
        self._state = "GROUND"
        return ("paste", text)

    def flush_timeout(self) -> list[Key]:
        """A read timeout expired mid-sequence: discard a dangling bare
        Escape, or terminate a paste whose closing marker never arrived —
        the content is kept (an insert beats a permanently wedged parser)."""
        if self._state in ("ESC", "CSI", "SS3"):
            self._state = "GROUND"
            self._csi = ""
        elif self._state == "PASTE":
            return [self._end_paste(self._paste)]
        return []


class Screen:
    """The terminal: a DECSTBM message region above a sticky footer
    (one divider row + 1..MAX_INPUT_ROWS input rows). Every write goes
    through one lock — the reader thread and the input thread both draw."""

    DIVIDER_HINT = "enter: send · alt-enter: newline"

    def __init__(
        self,
        *,
        fd: int | None = None,
        color: bool = False,
        out: TextIO | None = None,
        size: tuple[int, int] | None = None,
    ) -> None:
        self._fd = fd
        self._color = color
        self._out = out if out is not None else sys.stdout
        self._injected_size = size
        self._lock = threading.Lock()
        self._status = ""  # the divider's live text; empty → DIVIDER_HINT
        self._saved_attrs: list[int | list[bytes | int]] | None = None
        self.cols, self.rows = self._query_size()
        self.footer_rows = 2  # divider + one input row to start with
        self._cursor = (0, 0)  # visual (row, col) inside the input area
        self._window_top = 0

    def _query_size(self) -> tuple[int, int]:
        if self._injected_size is not None:
            return self._injected_size
        if self._fd is not None:
            size = os.get_terminal_size(self._fd)  # not $COLUMNS/$LINES
            return size.columns, size.lines
        s = shutil.get_terminal_size((120, 24))
        return s.columns, s.lines

    @property
    def region_bottom(self) -> int:
        return max(1, self.rows - self.footer_rows)

    @property
    def divider_row(self) -> int:
        return self.region_bottom + 1  # stays valid when rows < footer_rows

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()

    def _dim(self, text: str) -> str:
        return f"\x1b[2m{text}\x1b[0m" if self._color else text

    def _red(self, text: str) -> str:
        return f"\x1b[31m{text}\x1b[0m" if self._color else text

    # -- lifecycle ---------------------------------------------------------

    def setup(self) -> None:
        with self._lock:
            if self._fd is not None:
                self._saved_attrs = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                attrs = termios.tcgetattr(self._fd)
                attrs[3] &= ~termios.ISIG  # Ctrl-C arrives as byte 0x03
                # no Ctrl-S/Ctrl-Q output freeze; no CR→NL mapping — the key
                # parser must see paste bytes exactly as sent (\r\n vs \n)
                attrs[0] &= ~(termios.IXON | termios.ICRNL)
                termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
            self._write("\x1b[2J\x1b[H")
            self._write(f"\x1b[1;{self.region_bottom}r")
            self._write("\x1b[?2004h")  # bracketed paste
            self._draw_footer_locked("", 0)

    def teardown(self) -> None:
        with self._lock:
            self._write("\x1b[r\x1b[?2004l")
            self._write(f"\x1b[{self.rows};1H\n")
            if self._fd is not None and self._saved_attrs is not None:
                termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_attrs)
                self._saved_attrs = None

    # -- message area ------------------------------------------------------

    def print_lines(self, lines: list[str]) -> None:
        """Print rendered message lines at the region's bottom (scrolling it),
        then restore the cursor into the input."""
        if not lines:
            return
        with self._lock:
            self._write(f"\x1b[{self.region_bottom};1H")
            for line in lines:
                # 2K: a resize can leave stale footer rows inside the region;
                # a freshly scrolled-in row is blank, but these are not
                self._write("\x1b[2K" + line + "\n")
            self._cup_input_locked()

    def note(self, text: str) -> None:
        self.print_lines([self._dim(text)])

    def error(self, text: str) -> None:
        self.print_lines([self._red(text)])

    # -- footer ------------------------------------------------------------

    def redraw_footer(self, editor: Editor) -> None:
        with self._lock:
            disp = display_form(editor.buf)
            visual = wrap_rows(PROMPT + disp, self.cols)
            needed = 1 + min(MAX_INPUT_ROWS, len(visual))
            if needed != self.footer_rows:
                self._adjust_height_locked(needed)
            self._draw_footer_locked(disp, display_pos(editor.buf, editor.pos))

    def set_status(self, text: str) -> None:
        """Replace the divider hint with live status. Redraws that one row —
        `\\x1b[J` would erase the input rows below it, and `redraw_footer`
        would deadlock: this runs on the reader thread and the lock is plain."""
        with self._lock:
            self._status = text
            self._write(f"\x1b[{self.divider_row};1H\x1b[2K")
            self._write(self._divider_locked())
            self._cup_input_locked()

    def clear(self) -> None:
        with self._lock:
            self._write("\x1b[2J")

    def resize(self, size: tuple[int, int] | None = None) -> None:
        """Re-read the terminal size, erase the footer drawn for the old size
        (a border drag delivers one SIGWINCH per step — each would otherwise
        leave a stale divider+input pair behind), and re-set the
        region. The caller follows with `redraw_footer` — already-printed
        message lines do not reflow (accepted limitation)."""
        with self._lock:
            stale_divider = self.divider_row
            if size is not None:
                self.cols, self.rows = size
                self._injected_size = size
            else:
                self._injected_size = None
                self.cols, self.rows = self._query_size()
            self._write(f"\x1b[1;{self.region_bottom}r")
            self._write(f"\x1b[{stale_divider};1H\x1b[J")

    # -- internals (lock held) ----------------------------------------------

    def _adjust_height_locked(self, new_footer_rows: int) -> None:
        delta = new_footer_rows - self.footer_rows
        if delta > 0:
            # the footer covers delta more rows: scroll the region up first so
            # those message lines move into scrollback instead of being erased
            self._write(f"\x1b[{self.region_bottom};1H" + "\n" * delta)
            self.footer_rows = new_footer_rows
            self._write(f"\x1b[1;{self.region_bottom}r")
            self._write(f"\x1b[{self.divider_row};1H\x1b[J")
        else:
            stale_from = self.divider_row  # the old divider, now inside the region
            self.footer_rows = new_footer_rows
            self._write(f"\x1b[1;{self.region_bottom}r")
            self._write(f"\x1b[{stale_from};1H\x1b[J")
            # scroll the enlarged region down by the rows the grow path stole,
            # so the conversation ends at region_bottom-1 again (print_lines'
            # invariant) instead of leaving a permanent blank band; the blanks
            # enter at the region top and scroll away with the next messages
            self._write(f"\x1b[{-delta}T")

    def _divider_locked(self) -> str:
        hint = clip_cells(self._status or self.DIVIDER_HINT, max(0, self.cols - 2))
        rule = "─" * max(0, self.cols - cell_width(hint) - 1)
        line = (
            f"{rule}{hint}─" if self.cols >= cell_width(hint) + 2 else "─" * self.cols
        )
        return self._dim(line)

    def _draw_footer_locked(self, disp: str, pos: int) -> None:
        # `disp`/`pos` are in display form (display_form/display_pos) — the
        # terminal must never see raw buffer control bytes
        self._write(f"\x1b[{self.divider_row};1H\x1b[J")
        self._write(self._divider_locked())
        visual = wrap_rows(PROMPT + disp, self.cols)
        abs_row, abs_col = cursor_row_col(PROMPT + disp, len(PROMPT) + pos, self.cols)
        input_rows = self.footer_rows - 1
        top = min(self._window_top, max(0, len(visual) - input_rows))
        if abs_row < top:
            top = abs_row
        elif abs_row >= top + input_rows:
            top = abs_row - input_rows + 1
        self._window_top = top
        for i in range(input_rows):
            content = visual[top + i] if top + i < len(visual) else ""
            self._write(f"\x1b[{self.divider_row + 1 + i};1H\x1b[2K{content}")
        self._cursor = (abs_row - top, min(abs_col, self.cols - 1))
        self._cup_input_locked()

    def _cup_input_locked(self) -> None:
        row = self.divider_row + 1 + self._cursor[0]
        col = self._cursor[1] + 1
        self._write(f"\x1b[{row};{col}H")
