"""`moot log`: today's transcript, rendered deterministically.

Text mode is one line per record, each carrying the record's own timestamp;
markdown mode is a session document — a header, the messages in id order, an
event appendix; deliver records count in the header and are text-mode lines
only. Nothing is summarised. Nothing is created either: a home
without a transcripts directory is an error, not an empty rendering.

Only today's file is read (`<home>/transcripts/<today>.jsonl`), so a session
that crossed midnight is rendered in two halves.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from moot.core.clock import Clock
from moot.core.transcript import Transcript
from moot.spoke.render import message_line

# `session` and `session_end` carry no text and are never rendered as lines;
# `deliver` records are the who-got-what ledger and render as one line each.
_KINDS = {
    "msg": ("msg",),
    "event": ("event",),
    "deliver": ("deliver",),
    "all": ("msg", "event", "deliver"),
}

# Rendered as `k=v` after an event's name; the rest is the record's plumbing.
_EVENT_PLUMBING = ("type", "event", "ts")


def _hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _msg_id(rec: dict[str, Any]) -> int:
    """The record's message id, or 0 — the same rule the renderer applies to a
    delivered message: bools and non-positive ints are not ids."""
    value = rec.get("id")
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )


def _since_epoch(since: str, clock: Clock) -> float:
    """`HH:MM` today, on the clock the records were written with."""
    hour, minute = (int(part) for part in since.split(":"))
    day = datetime.fromtimestamp(clock.wall())
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def _select(
    records: list[dict[str, Any]],
    kind: str,
    since: str | None,
    last: int | None,
    clock: Clock,
) -> list[dict[str, Any]]:
    wanted = _KINDS[kind]
    selected = [r for r in records if r.get("type") in wanted]
    if since is not None:
        cut = _since_epoch(since, clock)
        selected = [r for r in selected if r["ts"] >= cut]
    if last is not None:
        selected = selected[-last:]
    return selected


def _event_line(rec: dict[str, Any]) -> str:
    rest = " ".join(f"{k}={v}" for k, v in rec.items() if k not in _EVENT_PLUMBING)
    head = f"{_hhmmss(rec['ts'])} [event] {rec['event']}"
    return f"{head} · {rest}" if rest else head


def _ids(ids: list[int]) -> str:
    return " ".join(f"#{i}" if i else "sys" for i in ids) or "—"


def _deliver_line(rec: dict[str, Any]) -> str:
    """`overheard` splits the frame into the wake and its context; a record
    written before that field existed lists the ids undivided."""
    head = f"{_hhmmss(rec['ts'])} [deliver r{rec['round']}] → {rec['to']}"
    if "overheard" not in rec:
        return f"{head} · {_ids(rec['msg_ids'])}"
    overheard = set(rec["overheard"])
    wake = [i for i in rec["msg_ids"] if i not in overheard]
    line = f"{head} · wake {_ids(wake)}"
    if overheard:
        line += f" · context {_ids(rec['overheard'])}"
    return line


def _msg_line(rec: dict[str, Any]) -> str:
    return f"{_hhmmss(rec['ts'])} " + message_line(
        rec["round"],
        _msg_id(rec),
        rec["from"],
        rec["to"],
        rec["kind"],
        rec["text"],
        private=bool(rec.get("private")),
    )


def _line(rec: dict[str, Any]) -> str:
    if rec["type"] == "msg":
        return _msg_line(rec)
    if rec["type"] == "deliver":
        return _deliver_line(rec)
    return _event_line(rec)


def _render_text(records: list[dict[str, Any]]) -> str:
    return "".join(f"{_line(rec)}\n" for rec in records)


def _participants(records: list[dict[str, Any]]) -> str:
    """Everyone who joined today, in join order, last kind and role winning."""
    seen: dict[str, str] = {}
    for rec in records:
        if rec.get("type") != "event" or rec.get("event") != "peer_joined":
            continue
        role = rec.get("role")
        clause = f"{rec['kind']}, {role}" if role else f"{rec['kind']}"
        seen[rec["name"]] = f"{rec['name']} ({clause})"
    return ", ".join(seen.values()) if seen else "none recorded"


def _span(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _md_header(all_records: list[dict[str, Any]], clock: Clock) -> str:
    """Counts and span describe the whole day's file, whatever `--kind`,
    `--since` or `--last` selected for the body. The wake counter is not
    reported: `/reset` zeroes it at will, so its peak says nothing."""
    day = datetime.fromtimestamp(clock.wall())
    n_msgs = sum(1 for r in all_records if r.get("type") == "msg")
    n_deliveries = sum(1 for r in all_records if r.get("type") == "deliver")
    stamps = [r["ts"] for r in all_records if isinstance(r.get("ts"), int | float)]
    head = [
        f"# moot session — {day.strftime('%Y-%m-%d')}",
        "",
        f"**Participants:** {_participants(all_records)}",
    ]
    today = f"**Today:** {n_msgs} messages · {n_deliveries} deliveries"
    if stamps:
        # ids order the body; the span is wall-clock, and a hub restart
        # re-seeds the id counter, so the two orders can disagree.
        first, last = min(stamps), max(stamps)
        head.append(
            f"{today} · {_hhmmss(first)} → {_hhmmss(last)} ({_span(last - first)})"
        )
    else:
        head.append(f"{today} · —")
    return "\n".join(head)


def _render_md(
    all_records: list[dict[str, Any]], records: list[dict[str, Any]], clock: Clock
) -> str:
    msgs = sorted(
        (r for r in records if r["type"] == "msg"), key=lambda r: r.get("id", 0)
    )
    events = [r for r in records if r["type"] == "event"]
    out = [_md_header(all_records, clock), ""]
    for rec in msgs:
        mid = _msg_id(rec)
        head = f"#{mid} " if mid else ""
        mark = " · private" if rec.get("private") else ""
        out.append(
            f"## {head}{rec['from']} → {rec['to']}{mark} · {rec['kind']}"
            f" (r{rec['round']})"
        )
        out.append("")
        out.append(rec["text"])
        out.append("")
    if events:
        out.append("## Events")
        out.append("")
        for rec in events:
            rest = " ".join(
                f"{k}={v}" for k, v in rec.items() if k not in _EVENT_PLUMBING
            )
            line = f"- {_hhmmss(rec['ts'])} {rec['event']}"
            out.append(f"{line} · {rest}" if rest else line)
        out.append("")
    return "\n".join(out)


def run_log(
    home: Path,
    kind: str,
    since: str | None,
    last: int | None,
    fmt: str,
    out: Path | None,
    clock: Clock | None = None,
) -> int:
    clock = clock or Clock()
    directory = home / "transcripts"
    if not directory.exists():
        # Checked before Transcript(), whose constructor would create it.
        print(f"moot: no transcripts at {directory}", file=sys.stderr)
        return 1
    records = Transcript(directory, clock).read_today()
    selected = _select(records, kind, since, last, clock)
    rendered = (
        _render_text(selected)
        if fmt == "text"
        else _render_md(records, selected, clock)
    )
    if out is not None:
        out.write_text(rendered, encoding="utf-8")
        return 0
    sys.stdout.write(rendered)
    return 0
