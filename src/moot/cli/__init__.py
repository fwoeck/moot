"""The `moot` command: the hub daemon, the observer seat, the four commands a
spoke gives its model (`stream`, `say`, `state`, `brief`), the `wait`/`peek`
file bridge, and the two operator commands (`log`, `doctor`).

Every client command resolves its state directory the same way (spoke/home.py)
and reaches the session's `moot stream` through its control socket.
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from moot.core import proto
from moot.spoke.brief import brief
from moot.spoke.ctl import ERR_NO_STREAM, ctl_call, ctl_path, resolve_session
from moot.spoke.home import resolve_home

RUNTIMES = ("claude-code", "opencode")


def _hhmm(raw: str) -> str:
    datetime.strptime(raw, "%H:%M")  # ValueError → argparse "invalid value"
    return raw


def _positive_int(raw: str) -> int:
    value = int(raw)  # ValueError → argparse "invalid value"
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return value


logger = logging.getLogger("moot.cli")

# A hook has five seconds; `state` gets the session id (≤ 1 s) and one ctl
# round trip, which the stream answers without waiting for the hub.
_STATE_TIMEOUT = 3.0

# `whoami` is answered from the stream's own registration, without the hub:
# the compact hook that runs `moot brief` has the same five seconds.
_WHOAMI_TIMEOUT = 1.5


def _build_parser() -> tuple[
    argparse.ArgumentParser, dict[str, argparse.ArgumentParser]
]:
    parser = argparse.ArgumentParser(prog="moot")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="start the hub daemon")
    serve_p.add_argument("--no-notify", action="store_true")
    serve_p.add_argument("--home", default=None)
    serve_p.add_argument("--transcripts", default=None, metavar="DIR")
    serve_p.add_argument("--max-rounds", type=_positive_int, default=None, metavar="N")

    observe_p = sub.add_parser("observe", help="the human seat")
    observe_p.add_argument("--home", default=None)
    observe_p.add_argument("--name", default="observer")
    observe_p.add_argument(
        "--full", action="store_true", help="do not cut statements to one line"
    )
    observe_p.add_argument(
        "--width", type=int, default=None, help="line width (default: terminal)"
    )
    observe_p.add_argument("--no-color", action="store_true")
    observe_p.add_argument(
        "--no-tui", action="store_true", help="plain line mode, no TUI footer"
    )

    stream_p = sub.add_parser("stream", help="hold the connection for a session")
    stream_p.add_argument("--home", default=None)
    stream_p.add_argument("--name", required=True)
    stream_p.add_argument("--kind", default="agent", help="e.g. claude-code, opencode")
    stream_p.add_argument("--role", default="")
    stream_p.add_argument("--session", default=None)
    stream_p.add_argument(
        "--inbox", default=None, help="append frames to this file instead of stdout"
    )

    say_p = sub.add_parser("say", help="send a message")
    say_p.add_argument("--home", default=None)
    say_p.add_argument("--kind", default="note", choices=sorted(proto.SAY_KINDS))
    say_p.add_argument("--session", default=None)
    say_p.add_argument(
        "--private",
        action="store_true",
        help="no other agent sees it (needs @NAME)",
    )
    say_p.add_argument(
        "words", nargs="*", metavar="TEXT", help="[@NAME|*] followed by the text"
    )

    state_p = sub.add_parser("state", help="report this session's state")
    state_p.add_argument("--home", default=None)
    state_p.add_argument("state", choices=sorted(proto.STATES))
    state_p.add_argument("--session", default=None)
    state_p.add_argument("detail", nargs="*", metavar="DETAIL")

    brief_p = sub.add_parser("brief", help="print the operating rules")
    brief_p.add_argument("--runtime", default="claude-code", choices=RUNTIMES)
    brief_p.add_argument("--name", default=None)
    brief_p.add_argument("--role", default="")
    brief_p.add_argument("--home", default=None)
    brief_p.add_argument("--session", default=None)

    log_p = sub.add_parser("log", help="render today's transcript")
    log_p.add_argument("--home", default=None)
    log_p.add_argument(
        "--kind",
        default="msg",
        choices=("msg", "event", "deliver", "all"),
        help="which records to render (all = msg + event + deliver, in file order)",
    )
    log_p.add_argument("--since", type=_hhmm, default=None, metavar="HH:MM")
    log_p.add_argument("--last", type=_positive_int, default=None, metavar="N")
    log_p.add_argument("--format", default="text", choices=("text", "md"))
    log_p.add_argument("--out", default=None, metavar="FILE")

    doctor_p = sub.add_parser("doctor", help="check the install and this home")
    doctor_p.add_argument("--home", default=None)
    doctor_p.add_argument(
        "--roster",
        action="store_true",
        help="also join as an observer and print the roster (touches the floor)",
    )

    wait_p = sub.add_parser("wait", help="block until the inbox has new lines")
    wait_p.add_argument("--home", default=None)
    wait_p.add_argument("--inbox", required=True)
    wait_p.add_argument("--session", default=None)
    wait_p.add_argument("--timeout", type=float, default=300.0)

    peek_p = sub.add_parser("peek", help="flush check before sending")
    peek_p.add_argument("--home", default=None)
    peek_p.add_argument("--inbox", required=True)
    peek_p.add_argument("--session", default=None)
    peek_p.add_argument("--settle", type=float, default=1.5)

    return parser, {"say": say_p, "state": state_p}


def _run_serve(args: argparse.Namespace) -> int:
    from moot.core.config import Config
    from moot.core.server import serve_main

    config = Config()
    if args.no_notify:
        config.notifications = False
    if args.home:
        # expanded like every client's --home (spoke/home.py), or a quoted
        # `~/…` would bind the hub somewhere the spokes never look
        config.home = Path(args.home).expanduser()
    if args.transcripts:
        config.transcripts = Path(args.transcripts).expanduser()
    if args.max_rounds is not None:
        config.max_rounds = args.max_rounds
    serve_main(config)
    return 0


def _run_observe(args: argparse.Namespace) -> int:
    from moot.spoke.observer import run_observer

    color = not args.no_color and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    return run_observer(
        resolve_home(args.home), args.name, args.full, args.width, color, args.no_tui
    )


def _run_stream(args: argparse.Namespace) -> int:
    from moot.cli.stream import run_stream

    # The ctl socket is keyed by session so hooks and tool calls of *this*
    # runtime session find it; a pane-run stream has no session and uses its
    # participant name.
    session = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or args.name
    return run_stream(
        resolve_home(args.home),
        args.name,
        args.kind,
        args.role,
        session,
        Path(args.inbox) if args.inbox else None,
    )


def _run_say(args: argparse.Namespace) -> int:
    from moot.cli.stream import SAY_TIMEOUT

    words: list[str] = args.words
    to = "*"
    if words and (words[0].startswith("@") or words[0] == "*"):
        to = words[0].removeprefix("@") or "*"
        words = words[1:]
    if args.private and to == "*":
        # the hub would answer `malformed`; the reason is worth the round trip
        print("moot: --private needs @NAME", file=sys.stderr)
        return 1
    session = resolve_session(args.session, read_stdin=False)
    frame: dict[str, object] = {
        "t": "say",
        "to": to,
        "kind": args.kind,
        "text": " ".join(words),
    }
    if args.private:
        frame["private"] = True
    reply = ctl_call(
        ctl_path(resolve_home(args.home), session),
        frame,
        # strictly wider than the stream's own window on the hub, so a hub
        # that never answers arrives here as the stream's `err timeout`
        SAY_TIMEOUT + 5.0,
    )
    if reply.get("t") == "ok":
        # `queued` is the number of recipients at which the message landed in
        # a queue because they were mid-turn (PROTOCOL); 0 means everyone
        # addressed got it delivered immediately.
        mid = reply.get("id")
        queued = reply.get("queued")
        parts = [f"ok → {to}"]
        if isinstance(mid, int) and not isinstance(mid, bool) and mid > 0:
            parts.append(f"#{mid}")
        if args.private:
            parts.append("private")
        if queued:
            parts.append(f"queued at {queued} busy peer(s)")
        print(" · ".join(parts))
        return 0
    return _report_err(reply, session)


def _run_state(args: argparse.Namespace) -> int:
    session = resolve_session(args.session, read_stdin=True)
    frame: dict[str, object] = {"t": "state", "state": args.state}
    detail = " ".join(args.detail)
    if detail:
        frame["detail"] = detail
    reply = ctl_call(ctl_path(resolve_home(args.home), session), frame, _STATE_TIMEOUT)
    if reply.get("t") == "ok":
        return 0
    return _report_err(reply, session)


def _report_err(reply: dict[str, object], session: str) -> int:
    if reply.get("code") == ERR_NO_STREAM:
        print(f"moot: no moot stream for session {session}", file=sys.stderr)
    else:
        line = f"err {reply.get('code')}: {reply.get('detail')}"
        retry_after = reply.get("retry_after")
        # the hub already rounds the wait to 3 decimals: print it as it came
        if retry_after is not None:
            line += f" · retry in {retry_after}s"
        print(line, file=sys.stderr)
    return 1


def _resolve_identity(args: argparse.Namespace) -> tuple[str | None, str, bool]:
    """`(name, role, lost)` for the brief: who this session is on the floor.

    `--name` is the answer whenever the caller has one. Without it — the
    SessionStart(compact) hook, which knows only the session id — the running
    `moot stream` is asked, and `lost` says there is none to ask.
    """
    if args.name:
        return args.name, args.role, False
    try:
        session = resolve_session(args.session, read_stdin=True)
        reply = ctl_call(
            ctl_path(resolve_home(args.home), session), {"t": "whoami"}, _WHOAMI_TIMEOUT
        )
    except Exception:
        # Sanctioned catch-and-report: this runs from a hook whose output is
        # the model's context, so a traceback would replace the rules it came
        # for. The brief still prints, one line poorer.
        logger.warning("brief: asking the stream who we are failed", exc_info=True)
        return None, args.role, True
    if reply.get("t") != "ok":
        return None, args.role, True
    name = reply.get("name")
    role = reply.get("role")
    return (
        name if isinstance(name, str) and name else None,
        args.role or (role if isinstance(role, str) else ""),
        False,
    )


def _read_goal(home: Path) -> str:
    """The session goal the hub persisted, or `""` — a floor without one, a
    home that is not this session's, and an unreadable file all read the same
    to a model: no goal to state."""
    from moot.core.hub import GOAL_FILE

    path = home / GOAL_FILE
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("brief: %s is unreadable", path, exc_info=True)
        return ""


def _run_brief(args: argparse.Namespace) -> int:
    name, role, lost = _resolve_identity(args)
    print(brief(args.runtime, name, role, _read_goal(resolve_home(args.home))))
    if lost:
        print("your floor connection is gone — ask the user to run /moot join again")
    return 0


def _run_log(args: argparse.Namespace) -> int:
    from moot.cli.log import run_log

    return run_log(
        resolve_home(args.home),
        args.kind,
        args.since,
        args.last,
        args.format,
        Path(args.out) if args.out else None,
    )


def _run_doctor(args: argparse.Namespace) -> int:
    from moot.cli.doctor import run_doctor

    return run_doctor(resolve_home(args.home), args.roster)


def _run_wait(args: argparse.Namespace) -> int:
    from moot.cli.inbox import run_wait

    return run_wait(
        resolve_home(args.home),
        resolve_session(args.session, read_stdin=False),
        Path(args.inbox),
        args.timeout,
    )


def _run_peek(args: argparse.Namespace) -> int:
    from moot.cli.inbox import run_peek

    return run_peek(
        resolve_home(args.home),
        resolve_session(args.session, read_stdin=False),
        Path(args.inbox),
        args.settle,
    )


def main() -> int:
    parser, intermixed = _build_parser()
    argv = sys.argv[1:]
    if argv and argv[0] in intermixed:
        # `say` and `state` carry free text after their positionals, so their
        # options must be allowed between them — which only
        # parse_intermixed_args does, and it refuses a parser with subparsers.
        args = intermixed[argv[0]].parse_intermixed_args(argv[1:])
        args.command = argv[0]
    else:
        args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    runners: dict[str, Callable[[argparse.Namespace], int]] = {
        "serve": _run_serve,
        "observe": _run_observe,
        "stream": _run_stream,
        "say": _run_say,
        "state": _run_state,
        "brief": _run_brief,
        "log": _run_log,
        "doctor": _run_doctor,
        "wait": _run_wait,
        "peek": _run_peek,
    }
    return runners[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
