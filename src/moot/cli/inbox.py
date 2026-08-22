"""`moot wait` / `moot peek`: the file bridge for runtimes without a push path.

`moot stream --inbox FILE` appends rendered frames to FILE; the model reads
them with these two commands through its shell tool. A cursor file next to
the inbox remembers what has been read. Both report the session's state
through the stream's control socket: a session that waits is idle (so the hub
flushes its queue), and busy again before it gets its messages back.
"""

import sys
import time
from pathlib import Path

from moot.spoke.ctl import ctl_call, ctl_path

_REPORT_TIMEOUT = 3.0


def _report(home: Path, session: str, state: str) -> None:
    reply = ctl_call(
        ctl_path(home, session), {"t": "state", "state": state}, _REPORT_TIMEOUT
    )
    if reply.get("t") != "ok":
        # The inbox is still worth reading, but the hub now has a stale idea
        # of this session — say so rather than swallowing it.
        print(
            f"moot: state {state} not reported "
            f"({reply.get('code')}: {reply.get('detail')})",
            file=sys.stderr,
        )


def _cursor_file(inbox: Path) -> Path:
    return inbox.with_name(inbox.name + ".cursor")


def _read_cursor(inbox: Path) -> int:
    cursor = _cursor_file(inbox)
    if not cursor.exists():
        return 0
    text = cursor.read_text(encoding="utf-8").strip()
    return int(text) if text else 0


def _consume_unread(inbox: Path, start: int) -> str:
    """Return everything after `start` and advance the cursor past it."""
    with inbox.open(encoding="utf-8") as fh:
        fh.seek(start)
        data = fh.read()
    _cursor_file(inbox).write_text(
        str(start + len(data.encode("utf-8"))), encoding="utf-8"
    )
    return data


def _size(inbox: Path) -> int:
    return inbox.stat().st_size if inbox.exists() else 0


def run_wait(home: Path, session: str, inbox: Path, timeout: float) -> int:
    start = _read_cursor(inbox)
    _report(home, session, "idle")  # a session that waits is idle: hub may flush
    deadline = time.monotonic() + timeout
    while True:
        if _size(inbox) > start:
            # Report busy first, then let the frame finish writing: by the
            # time the session acts, the spoke has forwarded the state and a
            # message arriving mid-turn is queued rather than delivered.
            _report(home, session, "busy")
            time.sleep(0.5)
            sys.stdout.write(_consume_unread(inbox, start))
            return 0
        if time.monotonic() >= deadline:
            print(f"(no new messages within {timeout:.0f}s — run wait again)")
            return 1
        time.sleep(0.5)


def run_peek(home: Path, session: str, inbox: Path, settle: float) -> int:
    """While a session is busy the hub queues its messages, so a plain inbox
    read would find nothing: briefly report idle (hub flushes), collect,
    report busy again (the session goes on composing/sending)."""
    start = _read_cursor(inbox)
    _report(home, session, "idle")
    deadline = time.monotonic() + settle
    size = 0
    while time.monotonic() < deadline:
        size = _size(inbox)
        if size > start:
            break
        time.sleep(0.1)
    _report(home, session, "busy")
    if size <= start:
        print("(nothing new)")
        return 1
    time.sleep(0.5)  # let a coalesced frame finish writing
    sys.stdout.write(_consume_unread(inbox, start))
    return 0
