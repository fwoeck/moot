"""The human seat: `moot observe`.

On a terminal this is a small TUI: a scrolling message area above a sticky
input footer with readline-style editing, a global history, and multi-line
composing. On pipes (and with
`--no-tui`) it is the classic line mode: one line per statement — timestamp,
a stable colour per sender, the addressee, the kind, and the text cut to the
terminal width (the full text is in the transcript).
"""

import os
import re
import select
import shutil
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from moot.core import proto
from moot.core.clock import Clock
from moot.core.config import Config
from moot.core.transcript import Transcript
from moot.spoke.conn import Conn, connect
from moot.spoke.tui import (
    MIN_COLS,
    MIN_ROWS,
    Editor,
    History,
    Key,
    KeyParser,
    Screen,
    cell_width,
    clip_cells,
    display_form,
    display_pos,
    wrap_rows,
)

# The hub drops NDJSON lines at Config.max_frame_bytes; measure the actual
# encoded frame (chars-vs-bytes was review finding f10) and refuse our own
# sends at the boundary instead of losing the connection to frame_too_large.
_MAX_SEND_BYTES = Config.max_frame_bytes

RING_CAP = 200  # messages the seat keeps for re-reading without the transcript
ROSTER_POLL_INTERVAL = 3.0  # seconds between status-band polls; read in the loop
# The commands the seat answers or composes itself. Everything in
# proto.COMMANDS is forwarded as a cmd frame; anything else is refused.
SEAT_COMMANDS = frozenset({"roster", "show", "last", "find", "q", "help", "close"})
QUOTE_EXCERPT = 500  # characters of a quoted message /q carries along


def parse_outgoing(line: str, seq: int) -> dict[str, object] | None:
    """`@beta text` → direct; `!done text` → kind done; `!private` → a private
    say (it needs a named peer — the caller refuses a private broadcast);
    else broadcast note. The prefixes work in any order, each at most once.
    `!state idle|busy|blocked [detail]` → a state frame (None if invalid)."""
    kind, to, text = "note", "*", line.strip()
    private = False
    if text.startswith("!state"):
        state, _, detail = text[6:].strip().partition(" ")
        if state not in proto.STATES:
            return None
        frame: dict[str, object] = {"t": "state", "state": state}
        if detail:
            frame["detail"] = detail.strip()
        return frame
    seen_kind = seen_to = seen_private = False
    while True:  # `!private`, `!kind` and `@name` in any order, each at most once
        if (text == "!private" or text.startswith("!private ")) and not seen_private:
            text = text[len("!private") :]
            private = seen_private = True
        elif text.startswith("!") and not seen_kind:
            word, _, text = text[1:].partition(" ")
            if word in proto.SAY_KINDS:
                kind = word
            seen_kind = True
        elif text.startswith("@") and not seen_to:
            to, _, text = text[1:].partition(" ")
            seen_to = True
        else:
            break
        text = text.lstrip()
    frame = {"t": "say", "to": to, "kind": kind, "text": text.strip(), "seq": seq}
    if private:
        frame["private"] = True
    return frame


class Pretty:
    PALETTE = ("36", "33", "35", "32", "34", "91")  # cyan yellow magenta green blue red

    def __init__(self, me: str, width: int | None, color: bool, full: bool) -> None:
        self.me = me
        self.width = width
        self.color = color
        self.full = full
        self._colors: dict[str, str] = {}

    def c(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def dim(self, text: str) -> str:
        return self.c("2", text)

    def who(self, name: str) -> str:
        if name == proto.SYSTEM_SENDER:
            return self.dim(name)
        if name not in self._colors:
            self._colors[name] = self.PALETTE[len(self._colors) % len(self.PALETTE)]
        return self.c(self._colors[name], name)

    def fit(self, prefix_len: int, text: str) -> str:
        text = " ".join(text.split())  # one line, collapsed whitespace
        width = self.width or shutil.get_terminal_size((120, 24)).columns
        # a very narrow seat still gets a bounded line (at least 20 body
        # cells, wrapping once) instead of hundreds of wrapped rows
        room = max(width - prefix_len, 20)
        if self.full or cell_width(text) <= room:
            return text
        # cut by display cells, not code points: an emoji is one char but two
        # cells, and an overflowing line wraps its ellipsis onto the next row
        kept: list[str] = []
        used = 0
        for ch in text:
            w = cell_width(ch)
            if used + w > room - 1:
                break
            kept.append(ch)
            used += w
        return "".join(kept) + "…"

    def lines(self, frame: dict[str, object]) -> list[str]:
        ts = time.strftime("%H:%M:%S")
        t = frame.get("t")
        if t == "deliver":
            return self._deliver(frame, ts)
        if t == "event":
            extra = " ".join(
                f"{k}={v}" for k, v in frame.items() if k not in ("t", "event")
            )
            return [self.dim(f"{ts} · {frame['event']} {extra}")]
        if t == "err":
            line = f"{ts} ! {frame.get('code')}: {frame.get('detail')}"
            retry_after = frame.get("retry_after")
            if retry_after is not None:
                line += f" (retry in {retry_after}s)"
            return [self.c("31", line)]
        if t == "roster":
            return [self._roster(frame, ts)]
        return []

    def _msg_lines(
        self,
        ts: str,
        sender: str,
        to: str,
        kind: str,
        text: str,
        suffix_plain: str,
    ) -> list[str]:
        """One message as display lines. Default: ONE line — newlines collapse
        to spaces and the text is cut to the width (the transcript holds the
        full text). With --full: every line, LFs preserved, nothing cut."""
        to_disp = "you" if to == self.me else to
        head_plain = f"{ts} {sender} → {to_disp} {kind}  "
        arrow = self.c("1", "→ you") if to == self.me else f"→ {to_disp}"
        head = f"{self.dim(ts)} {self.who(sender)} {arrow} {self.dim(kind)}  "
        if not self.full:
            body = self.fit(len(head_plain) + len(suffix_plain), text)
            return [head + body + self.dim(suffix_plain)]
        segments = [" ".join(seg.split()) for seg in text.split("\n")]
        lines = []
        for i, seg in enumerate(segments):
            prefix = head if i == 0 else " " * len(head_plain)
            tail = suffix_plain if i == len(segments) - 1 else ""
            body = self.fit(len(head_plain) + len(tail), seg)
            lines.append(prefix + body + self.dim(tail))
        return lines

    def echo_lines(
        self,
        to: str,
        kind: str,
        text: str,
        queued: int = 0,
        ts: str | None = None,
        msg_id: int | None = None,
        private: bool = False,
    ) -> list[str]:
        """The observer's own message, rendered on hub `ok` (no round suffix —
        the `ok` frame does not carry one; the id it assigned does follow)."""
        ts = ts or time.strftime("%H:%M:%S")
        suffix = f"  #{msg_id}" if msg_id else ""
        if private:
            suffix += " · private"
        if queued:
            suffix += f" · queued at {queued} busy peer(s)"
        return self._msg_lines(ts, self.me, to, kind, text, suffix)

    def replay_lines(self, rec: dict[str, Any], full: bool = False) -> list[str]:
        """A stored message replayed from the ring or today's transcript: its
        own timestamp, the id it is quoted by, dim. `full` keeps the whole
        text, wrapped to the width."""
        ts = time.strftime("%H:%M:%S", time.localtime(rec["ts"]))
        mark = " · private" if rec.get("private") else ""
        head_plain = (
            f"#{rec['id']} {ts} {rec['from']} → {rec['to']}{mark} · {rec['kind']}: "
        )
        head = (
            self.dim(f"#{rec['id']} {ts} ")
            + self.who(str(rec["from"]))
            + self.dim(f" → {rec['to']}{mark} · {rec['kind']}: ")
        )
        text = " ".join(str(rec["text"]).split())
        if not full:
            return [head + self.dim(self.fit(len(head_plain), text))]
        width = self.width or shutil.get_terminal_size((120, 24)).columns
        rows = wrap_rows(text, max(20, width - len(head_plain)))
        return [
            (head if i == 0 else " " * len(head_plain)) + self.dim(row)
            for i, row in enumerate(rows)
        ]

    def _deliver(self, frame: dict[str, object], ts: str) -> list[str]:
        out = []
        round_suffix = f"  r{frame['round']}"
        for m in _items(frame, "msgs"):
            mid = _msg_id(m)
            suffix = round_suffix + (f" #{mid}" if mid else "")
            if m.get("private") is True:
                suffix += " · private"
            out.extend(
                self._msg_lines(ts, m["from"], m["to"], m["kind"], m["text"], suffix)
            )
        return out

    def _roster(self, frame: dict[str, object], ts: str) -> str:
        peers = ", ".join(
            f"{self.who(p['name'])}:{p['state']}"
            + (" done" if p.get("finished") else "")
            for p in _items(frame, "peers")
        )
        flags = (" frozen" if frame.get("frozen") else "") + (
            " muted" if frame.get("muted") else ""
        )
        goal = _str_or_empty(frame.get("goal"))
        tail = f"  goal: {goal}" if goal else ""
        return f"{self.dim(ts)} roster r{frame['round']}{flags}  {peers}{tail}"


def _items(frame: dict[str, object], key: str) -> list[Any]:
    value = frame.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{frame.get('t')!r} frame without a {key} list")
    return value


def _msg_id(m: dict[str, Any]) -> int | None:
    """The hub's message id, or None for the id-less placeholders (a dropped
    queue slot, a message rebuilt from a transcript record)."""
    mid = m.get("id")
    return (
        mid if isinstance(mid, int) and not isinstance(mid, bool) and mid > 0 else None
    )


class Seat:
    """What the seat knows: the recent messages, who is on the floor, the
    goal, and the sequence numbers of everything it sends.

    The reader thread fills it from inbound frames; the input thread reads it
    while composing. Never call into `Screen` while holding this lock —
    build the lines, release, then draw.
    """

    def __init__(self, me: str, home: Path) -> None:
        self.me = me
        self.home = home
        self.goal = ""
        self.pending: dict[int, tuple[str, str, str, bool]] = {}  # say seq → echo
        self._lock = threading.Lock()
        self._ring: deque[dict[str, Any]] = deque(maxlen=RING_CAP)
        self._peers: dict[str, str] = {}  # name → kind
        self._transcript: Transcript | None = None
        self._seq = 0  # say/cmd seqs: 1, 2, 3, …
        self._poll_seq = 0  # roster polls: -1, -2, … (never a typed line's seq)

    # -- sequence numbers --------------------------------------------------

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def next_poll_seq(self) -> int:
        with self._lock:
            self._poll_seq -= 1
            return self._poll_seq

    # -- what the reader thread feeds in -----------------------------------

    def observe_welcome(self, welcome: dict[str, object]) -> None:
        with self._lock:
            self._peers = {p["name"]: p["kind"] for p in _items(welcome, "peers")}
            self.goal = _str_or_empty(welcome.get("goal"))

    def observe(self, frame: dict[str, object]) -> None:
        t = frame.get("t")
        if t == "deliver":
            with self._lock:
                for m in _items(frame, "msgs"):
                    mid = _msg_id(m)
                    if mid is not None:
                        self._ring.append(_ring_entry(mid, m))
        elif t == "event":
            self._observe_event(frame)
        elif t == "roster":
            with self._lock:
                # the roster lists us as well; the seat is not its own peer
                self._peers = {
                    p["name"]: p["kind"]
                    for p in _items(frame, "peers")
                    if p["name"] != self.me
                }
                self.goal = _str_or_empty(frame.get("goal"))

    def _observe_event(self, frame: dict[str, object]) -> None:
        event = frame.get("event")
        name = frame.get("name")
        with self._lock:
            if event == "peer_joined" and isinstance(name, str):
                self._peers[name] = _str_or_empty(frame.get("kind"))
            elif event == "peer_left" and isinstance(name, str):
                self._peers.pop(name, None)
            elif event == "goal_set":
                self.goal = _str_or_empty(frame.get("text"))

    def record_own(
        self, msg_id: int, to: str, kind: str, text: str, private: bool = False
    ) -> None:
        """The seat's own accepted say — the hub never routes it back to us."""
        with self._lock:
            self._ring.append(
                {
                    "id": msg_id,
                    "from": self.me,
                    "to": to,
                    "kind": kind,
                    "text": text,
                    "ts": time.time(),
                    "private": private,
                }
            )

    # -- what the input thread asks ----------------------------------------

    def lookup(self, msg_id: int) -> dict[str, Any] | None:
        with self._lock:
            for entry in reversed(self._ring):
                if entry["id"] == msg_id:
                    return entry
        for rec in self._transcript_msgs():
            if rec["id"] == msg_id:
                return rec
        return None

    def recent(self, n: int) -> list[dict[str, Any]]:
        """The last `n` messages, ring first and today's transcript behind it."""
        with self._lock:
            entries = list(self._ring)[-n:]
        if len(entries) >= n:
            return entries
        seen = {e["id"] for e in entries}
        older = [r for r in self._transcript_msgs() if r["id"] not in seen]
        entries = (older + entries)[-n:]
        entries.sort(key=lambda e: e["id"])
        return entries

    def search(self, rx: re.Pattern[str], limit: int) -> list[dict[str, Any]]:
        """Ring only — a regex over the whole day's file per `/find` is not
        worth it; `/help` says so."""
        with self._lock:
            entries = [e for e in self._ring if rx.search(e["text"])]
        return entries[-limit:]

    def known_peer(self, name: str) -> bool:
        if name in ("*", self.me):
            return True
        with self._lock:
            return name in self._peers

    def peer_names(self) -> list[str]:
        with self._lock:
            return sorted(self._peers)

    # -- today's transcript ------------------------------------------------

    def _transcript_msgs(self) -> list[dict[str, Any]]:
        transcript = self._todays_transcript()
        if transcript is None:
            return []
        return [
            _ring_entry(r["id"], r)
            for r in transcript.read_today()
            if r.get("type") == "msg" and _msg_id(r) is not None
        ]

    def _todays_transcript(self) -> Transcript | None:
        """Lazy: `Transcript.__init__` creates directories, and a seat pointed
        at a mistyped home must not make one."""
        if self._transcript is None:
            directory = self.home / "transcripts"
            if not directory.exists():
                return None
            self._transcript = Transcript(directory, Clock())
        return self._transcript


def _str_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _ring_entry(msg_id: int, m: dict[str, Any]) -> dict[str, Any]:
    """One message in the seat's own shape — the transcript's `msg` record and
    a `deliver` message normalise to the same keys."""
    ts = m.get("ts")
    return {
        "id": msg_id,
        "from": m["from"],
        "to": m["to"],
        "kind": m["kind"],
        "text": m["text"],
        "ts": ts if isinstance(ts, int | float) else 0.0,
        "private": m.get("private") is True,
    }


@dataclass
class Outgoing:
    """What one typed line becomes: frames to send (with the echo they are
    answered by), lines to print locally, or a refusal that keeps the buffer."""

    frames: list[dict[str, object]] = field(default_factory=list)
    echo: dict[int, tuple[str, str, str, bool]] = field(default_factory=dict)
    local: list[str] = field(default_factory=list)
    error: str | None = None
    ignored: bool = False


def build_outgoing(line: str, seat: Seat, max_rows: int, pretty: Pretty) -> Outgoing:
    """One typed line → what the seat does with it. A refusal consumes no
    sequence number, so the next accepted line still gets the next seq."""
    ctrl = proto.control_char_pos(line)
    if ctrl is not None:
        col = display_pos(line, ctrl) + 1
        return Outgoing(error=f"[control characters at col {col} — not sent]")
    if line.startswith("/"):
        cmd, _, arg = line[1:].partition(" ")
        return _command(cmd, arg.strip(), seat, max_rows, pretty)
    frame = parse_outgoing(line, 0)
    if frame is None:
        return Outgoing(ignored=True)
    if frame.get("t") != "say":
        return Outgoing(frames=[frame])
    return _say(frame, seat)


def _say(frame: dict[str, object], seat: Seat) -> Outgoing:
    to, kind, text = str(frame["to"]), str(frame["kind"]), str(frame["text"])
    private = frame.get("private") is True
    if not text:
        return Outgoing(error="[empty message — not sent]")
    if private and to == "*":
        return Outgoing(error="[private needs @name]")
    if not seat.known_peer(to):
        return Outgoing(error=_no_peer(to, seat))
    seq = seat.next_seq()
    frame["seq"] = seq
    return Outgoing(frames=[frame], echo={seq: (to, kind, text, private)})


HELP_LINES = (
    "text → broadcast · @name text → direct · !kind text → claim, question,",
    "  answer, result, objection, done, note · @name !private text → only that",
    "  peer and the observers see it · prefixes in any order",
    "!state idle|busy|blocked [detail] → set your own state",
    "hub commands: /goal TEXT /freeze /resume [n] /reset /mute /unmute /roster",
    "  /close TEXT (says done, then resets the round budget)",
    "local commands (never reach the hub): /show #N /last [n] /find REGEX",
    "  /q N @name text (quote #N in a question; a private #N stays private)",
    "  /help",
    "/find searches this seat's buffer; /show and /last also read today's",
    "  transcript",
)


def _command(cmd: str, arg: str, seat: Seat, max_rows: int, pretty: Pretty) -> Outgoing:
    if cmd not in SEAT_COMMANDS and cmd not in proto.COMMANDS:
        return Outgoing(error=_unknown_command(cmd))
    if cmd == "show":
        return _show(arg, seat, max_rows, pretty)
    if cmd == "last":
        return _last(arg, seat, max_rows, pretty)
    if cmd == "find":
        return _find(arg, seat, max_rows, pretty)
    if cmd == "q":
        return _quote(arg, seat)
    if cmd == "help":
        return Outgoing(local=[pretty.dim(line) for line in HELP_LINES])
    if cmd == "close":
        return _close(arg, seat)
    if cmd == "roster":
        return Outgoing(frames=[{"t": "roster", "seq": seat.next_seq()}])
    if cmd == "goal" and not arg:
        return Outgoing(error="[/goal needs a text]")
    frame: dict[str, object] = {"t": "cmd", "cmd": cmd, "seq": seat.next_seq()}
    if cmd == "resume" and arg.isdigit():
        frame["args"] = {"n": int(arg)}
    if cmd == "goal":
        frame["args"] = {"text": arg}
    return Outgoing(frames=[frame])


def _msg_id_arg(arg: str) -> int | None:
    """`#41` and `41` are the same handle; anything else is not an id."""
    digits = arg[1:] if arg.startswith("#") else arg
    return int(digits) if digits.isdigit() else None


def _replay(
    records: list[dict[str, Any]], max_rows: int, pretty: Pretty, full: bool = False
) -> Outgoing:
    """Replayed rows, newest kept, closed by a marker naming what was cut."""
    rows: list[str] = []
    for rec in records:
        rows.extend(pretty.replay_lines(rec, full))
    cap = max(1, max_rows)
    dropped = max(0, len(rows) - cap)
    if dropped:
        rows = rows[-cap:]
        rows.append(pretty.dim(f"[+{dropped} more rows — transcript]"))
    else:
        rows.append(pretty.dim("[end of replay]"))
    return Outgoing(local=rows)


def _show(arg: str, seat: Seat, max_rows: int, pretty: Pretty) -> Outgoing:
    msg_id = _msg_id_arg(arg)
    if msg_id is None:
        return Outgoing(error="[/show takes a message id]")
    rec = seat.lookup(msg_id)
    if rec is None:
        return Outgoing(
            error=f"[no message #{msg_id} — not in this seat's buffer "
            "or today's transcript]"
        )
    return _replay([rec], max_rows, pretty, full=True)


def _last(arg: str, seat: Seat, max_rows: int, pretty: Pretty) -> Outgoing:
    if arg and not arg.isdigit():
        return Outgoing(error="[/last takes a count]")
    records = seat.recent(int(arg) if arg else 3)
    if not records:
        return Outgoing(local=[pretty.dim("[nothing to replay]")])
    return _replay(records, max_rows, pretty)


def _find(arg: str, seat: Seat, max_rows: int, pretty: Pretty) -> Outgoing:
    if not arg:
        return Outgoing(error="[/find needs a regex]")
    try:
        rx = re.compile(arg)
    except re.error as exc:
        return Outgoing(error=f"[bad regex: {exc}]")
    records = seat.search(rx, max_rows)
    if not records:
        return Outgoing(local=[pretty.dim("[no match]")])
    return _replay(records, max_rows, pretty)


def _quote(arg: str, seat: Seat) -> Outgoing:
    ident, _, rest = arg.partition(" ")
    msg_id = _msg_id_arg(ident)
    if msg_id is None:
        return Outgoing(error="[/q takes a message id, then @name and a question]")
    rec = seat.lookup(msg_id)
    if rec is None:
        return Outgoing(error=f"[no message #{msg_id}]")
    name, _, human = rest.strip().partition(" ")
    name = name[1:] if name.startswith("@") else name
    if not seat.known_peer(name):
        return Outgoing(error=_no_peer(name, seat))
    human = human.strip()
    if not human:
        return Outgoing(error="[/q needs a question after @name]")
    quoted = str(rec["text"])
    excerpt = quoted if len(quoted) <= QUOTE_EXCERPT else quoted[:QUOTE_EXCERPT] + "…"
    text = f'{rec["from"]} wrote (#{rec["id"]}): "{excerpt}"\n{human}'
    private = rec.get("private") is True  # a quoted whisper stays a whisper
    if private and name == "*":
        return Outgoing(
            error="[private needs @name — a private #N cannot be broadcast]"
        )
    seq = seat.next_seq()
    frame: dict[str, object] = {
        "t": "say",
        "to": name,
        "kind": "question",
        "text": text,
        "seq": seq,
        **({"private": True} if private else {}),
    }
    return Outgoing(frames=[frame], echo={seq: (name, "question", text, private)})


def _close(arg: str, seat: Seat) -> Outgoing:
    """Announce and reset the budget: a broadcast `done`, then `reset`. The
    hub never marks an observer finished, so this fires no session_done."""
    if not arg:
        return Outgoing(error="[/close needs a closing statement]")
    say_seq = seat.next_seq()
    cmd_seq = seat.next_seq()
    return Outgoing(
        frames=[
            {"t": "say", "to": "*", "kind": "done", "text": arg, "seq": say_seq},
            {"t": "cmd", "cmd": "reset", "seq": cmd_seq},
        ],
        echo={say_seq: ("*", "done", arg, False)},
    )


def _unknown_command(cmd: str) -> str:
    known = " ".join(f"/{c}" for c in ("freeze", "resume", "reset", "mute", "unmute"))
    local = " ".join(f"/{c}" for c in ("goal", "roster", "show", "last", "find"))
    return f"[unknown command '/{cmd}' — {known} {local} /q /close /help]"


def _no_peer(name: str, seat: Seat) -> str:
    return f"[no peer {name!r} — {', '.join(seat.peer_names())}]"


def _dur(seconds: float) -> str:
    total = int(seconds)
    return f"{total}s" if total < 60 else f"{total // 60}m{total % 60:02d}s"


def status_band(roster: dict[str, object], width: int, me: str = "") -> str:
    """The floor in one row: this seat's own name, the round, every agent
    with its state, the flags, the goal. Other observers are not floor
    information; the own name is — it is what the agents address."""
    max_rounds = roster.get("max_rounds")
    head = f"r{roster['round']}"
    if max_rounds is not None:
        head += f"/{max_rounds}"
    segments = [f"@{me}", head] if me else [head]
    segments += [
        _peer_segment(p) for p in _items(roster, "peers") if p.get("kind") != "observer"
    ]
    if roster.get("frozen"):
        segments.append("frozen")
    if roster.get("muted"):
        segments.append("muted")
    goal = _str_or_empty(roster.get("goal"))
    if goal:
        segments.append(f"goal: {goal}")
    return clip_cells(" · ".join(segments), max(0, width - 2))


def _peer_segment(p: dict[str, Any]) -> str:
    state = str(p["state"])
    detail = p.get("blocked_detail")
    if state == "blocked" and isinstance(detail, str) and detail:
        state = f"blocked({clip_cells(detail, 20)})"
    segment = f"{p['name']} {state}"
    busy_for = p.get("busy_for")
    if p["state"] != "idle" and isinstance(busy_for, int | float) and busy_for > 0:
        segment += f" {_dur(busy_for)}"
    queued = p.get("queued")
    if isinstance(queued, int) and queued:
        senders = ",".join(str(name) for name in p.get("queued_from", []))
        segment += f" {queued}q({senders})"
    context = p.get("context")
    if isinstance(context, int) and context:
        segment += f" {context}c"
    if p.get("finished"):
        segment += " done"
    return segment


def _reader_loop(
    conn: Conn, pretty: Pretty, closed: threading.Event, seat: Seat
) -> None:
    for frame in conn.frames():
        if frame.get("t") == "ping":
            conn.send({"t": "pong"})
            continue
        seat.observe(frame)
        for text in pretty.lines(frame):
            print(text, flush=True)
    closed.set()
    print("[hub closed the connection]", flush=True)


def _welcome_line(pretty: Pretty, name: str, welcome: dict[str, object]) -> str:
    limits = welcome.get("limits")
    if not isinstance(limits, dict):
        raise ValueError(f"welcome without limits: {welcome!r}")
    goal = _str_or_empty(welcome.get("goal"))
    return (
        pretty.dim(time.strftime("%H:%M:%S"))
        + f" welcome {name}  round {welcome['round']}  peers "
        + ", ".join(pretty.who(p["name"]) for p in _items(welcome, "peers"))
        + f"  limits {limits['rate']}, {limits['max_rounds']} rounds"
        + (f"  goal: {goal}" if goal else "")
    )


def run_observer(
    home: Path,
    name: str,
    full: bool,
    width: int | None,
    color: bool,
    no_tui: bool = False,
) -> int:
    """Run the seat until stdin ends; exit code 1 if the hub hung up first."""
    if no_tui or not _tui_available():
        return _run_classic(home, name, full, width, color)
    return _run_tui(home, name, full, width, color)


def _tui_available() -> bool:
    """The TUI needs two TTYs, a usable TERM, and some room; anything less
    gets the classic line mode (a human-visible reason goes to stderr)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if os.environ.get("TERM") in (None, "", "dumb"):
        print("moot: TERM unset or dumb — plain mode", file=sys.stderr)
        return False
    size = shutil.get_terminal_size((120, 24))
    if size.columns < MIN_COLS or size.lines < MIN_ROWS:
        print(
            f"moot: terminal below {MIN_COLS}x{MIN_ROWS} — plain mode", file=sys.stderr
        )
        return False
    return True


def _run_classic(
    home: Path, name: str, full: bool, width: int | None, color: bool
) -> int:
    conn, welcome = connect(home, name, "observer", "human observer", [])
    pretty = Pretty(name, width, color, full)
    seat = Seat(name, home)
    seat.observe_welcome(welcome)
    print(_welcome_line(pretty, name, welcome), flush=True)
    closed = threading.Event()
    reader = threading.Thread(
        target=_reader_loop, args=(conn, pretty, closed, seat), daemon=True
    )
    reader.start()
    max_rows = shutil.get_terminal_size((120, 24)).lines - 2
    for line in sys.stdin:
        if closed.is_set():
            print("[hub is gone — nothing sent]", flush=True)
            return 1
        line = line.strip()
        if not line:
            continue
        out = build_outgoing(line, seat, max_rows, pretty)
        if out.error is not None:
            print(out.error, flush=True)
            continue
        if out.ignored:
            print(f"[ignored] {line}", flush=True)
            continue
        for text in out.local:
            print(text, flush=True)
        for frame in out.frames:
            conn.send(frame)
    if closed.is_set():
        return 1
    conn.send({"t": "bye"})
    reader.join(timeout=2.0)  # the hub closes on bye; the loop then ends
    return 0


def _run_tui(home: Path, name: str, full: bool, width: int | None, color: bool) -> int:
    conn, welcome = connect(home, name, "observer", "human observer", [])
    pretty = Pretty(name, width, color, full)
    history = History(Path.home() / ".moot" / "observer_history")
    history_failed = False
    startup_note: str | None = None
    try:
        entries = history.load()
    except (OSError, UnicodeDecodeError) as exc:
        entries = []
        history_failed = True  # loud once, then degrade
        startup_note = f"[history: {exc}]"
    editor = Editor(entries)
    parser = KeyParser()
    screen = Screen(fd=sys.stdin.fileno(), color=color)
    seat = Seat(name, home)
    seat.observe_welcome(welcome)
    closed = threading.Event()
    teardown_started = threading.Event()
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)

    def wake(tag: bytes) -> None:
        if teardown_started.is_set():
            return  # the pipe fds are (about to be) closed and may be reused
        try:
            os.write(wake_w, tag)
        except OSError:
            pass  # a full or closing pipe just coalesces wakeups

    def reader() -> None:
        try:
            for frame in conn.frames():
                t = frame.get("t")
                if t == "ping":
                    conn.send({"t": "pong"})
                    continue
                if t == "ok":
                    seq_of = frame.get("seq")
                    info = (
                        seat.pending.pop(seq_of, None)
                        if isinstance(seq_of, int)
                        else None
                    )
                    if info is not None:
                        q = frame.get("queued")
                        queued = q if isinstance(q, int) else 0
                        mid = _msg_id(frame)
                        to, kind, text, private = info
                        screen.print_lines(
                            pretty.echo_lines(
                                to, kind, text, queued, msg_id=mid, private=private
                            )
                        )
                        if mid is not None:
                            seat.record_own(mid, to, kind, text, private)
                    continue
                if t == "err":
                    seq_of = frame.get("seq")
                    if isinstance(seq_of, int):
                        seat.pending.pop(seq_of, None)
                if t == "roster":
                    seq_of = frame.get("seq")
                    if isinstance(seq_of, int) and seq_of < 0:
                        # our own poll: the divider, never a message line
                        seat.observe(frame)
                        screen.set_status(status_band(frame, screen.cols, seat.me))
                        continue
                seat.observe(frame)
                lines = pretty.lines(frame)
                if lines:
                    screen.print_lines(lines)
        except Exception as exc:
            # a dying reader must surface as seat state, not as a daemon
            # thread's half-written traceback over the raw-mode footer
            closed.set()
            if not teardown_started.is_set():
                screen.error(f"[reader failed: {exc!r}]")
            wake(b"C")
            return
        closed.set()
        if not teardown_started.is_set():
            screen.note("[hub closed the connection]")
        wake(b"C")

    reader_thread = threading.Thread(target=reader, daemon=True)

    def on_winch(signum: int, frame: FrameType | None) -> None:
        wake(b"R")

    previous_winch: Any = None
    try:
        screen.setup()
        # SIGWINCH handling needs the main thread (production: always; tests:
        # the seat may run in a worker thread — resize then waits for a key)
        if threading.current_thread() is threading.main_thread():
            previous_winch = signal.signal(signal.SIGWINCH, on_winch)
        screen.print_lines([_welcome_line(pretty, name, welcome)])
        if startup_note is not None:
            screen.error(startup_note)
        reader_thread.start()
        quit_seat = False
        stdin_fd = sys.stdin.fileno()
        next_poll = time.monotonic()
        while not quit_seat:
            key_wait = True  # a timeout that belongs to the key parser
            if parser.pending_escape:
                timeout: float | None = 0.05
            elif parser.pending_paste:
                timeout = 2.0  # a stalled paste ends as an insert, not a wedge
            else:
                key_wait = False
                timeout = max(0.0, next_poll - time.monotonic())
            ready, _, _ = select.select([stdin_fd, wake_r], [], [], timeout)
            if wake_r in ready:  # before the keys: a C tag must stop dispatch
                for tag in os.read(wake_r, 64):
                    if tag == ord("R"):
                        screen.resize()
                        screen.redraw_footer(editor)
                    elif tag == ord("C"):
                        quit_seat = True
            keys: list[Key] = []
            if not quit_seat and stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if not data:
                    quit_seat = True  # stdin EOF, like the classic loop's end
                    continue
                keys.extend(parser.feed(data))
            if not ready and key_wait:
                keys.extend(parser.flush_timeout())
            if not key_wait and time.monotonic() >= next_poll:
                # never while a key parse is pending: the poll deadline must
                # not end a paste early (flush_timeout stays the parser's).
                # The deadline moves on even with the hub gone — it is what
                # bounds this loop's select.
                next_poll = time.monotonic() + ROSTER_POLL_INTERVAL
                if not closed.is_set():
                    try:
                        conn.send({"t": "roster", "seq": seat.next_poll_seq()})
                    except OSError:
                        pass  # the reader thread owns hangup reporting
            for key in keys:
                kind = key[0]
                if kind in ("char", "paste"):
                    editor.insert(key[1])
                elif kind == "alt_enter":
                    editor.newline()
                elif kind == "enter":
                    stripped = editor.buf.strip()
                    out = (
                        build_outgoing(stripped, seat, screen.region_bottom - 2, pretty)
                        if stripped
                        else None
                    )
                    if out is not None and out.error is not None:
                        screen.error(out.error)  # buffer is kept
                    elif out is not None and any(
                        len(proto.encode(f)) >= _MAX_SEND_BYTES for f in out.frames
                    ):
                        screen.error("[too long — not sent]")  # buffer is kept
                    else:
                        text = editor.submit()
                        if stripped and out is not None:
                            if not history_failed:
                                try:
                                    history.append(text)
                                except (OSError, UnicodeDecodeError) as exc:
                                    history_failed = True
                                    screen.error(f"[history: {exc}]")
                            if out.ignored:
                                # display form: this is the one message-area
                                # sink for buffer text the hub never validated
                                screen.note(f"[ignored] {display_form(text)}")
                            screen.print_lines(out.local)
                            if out.frames and closed.is_set():
                                screen.error("[hub is gone — nothing sent]")
                            elif out.frames:
                                # before the send: the ok can reach the reader
                                # thread before conn.send returns here
                                seat.pending.update(out.echo)
                                for frame_out in out.frames:
                                    conn.send(frame_out)
                elif kind == "key":
                    action = key[1]
                    if action == "left":
                        editor.left()
                    elif action == "right":
                        editor.right()
                    elif action == "home":
                        editor.home()
                    elif action == "end":
                        editor.end()
                    elif action == "backspace":
                        editor.backspace()
                    elif action == "delete":
                        editor.delete()
                    elif action == "up":
                        editor.history_prev()
                    elif action == "down":
                        editor.history_next()
                    elif action == "ctrl_a":
                        editor.home()
                    elif action == "ctrl_e":
                        editor.end()
                    elif action == "ctrl_k":
                        editor.kill_to_end()
                    elif action == "ctrl_u":
                        editor.kill_all()
                    elif action == "ctrl_w":
                        editor.kill_word_back()
                    elif action == "alt_b":
                        editor.word_left()
                    elif action == "alt_f":
                        editor.word_right()
                    elif action == "ctrl_l":
                        screen.clear()
                    elif action == "ctrl_c":
                        if editor.buf:
                            editor.kill_all()
                        else:
                            quit_seat = True
                    elif action == "ctrl_d":
                        if editor.buf:
                            editor.delete()
                        else:
                            quit_seat = True
                if quit_seat:
                    break
                screen.redraw_footer(editor)
        if closed.is_set():
            return 1
        conn.send({"t": "bye"})
        reader_thread.join(timeout=2.0)  # the hub closes on bye
        return 0
    finally:
        teardown_started.set()
        if previous_winch is not None:
            signal.signal(signal.SIGWINCH, previous_winch)
        screen.teardown()
        os.close(wake_r)
        os.close(wake_w)
        conn.close()  # shutdown-first (Conn.close): also unblocks a parked reader
