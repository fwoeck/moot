"""Wire protocol: frame validation, error codes, encoding.

Normative companion to docs/PROTOCOL.md. NDJSON: exactly one JSON object
per line, '\n'-terminated, UTF-8. Frames larger than the configured maximum are
rejected with err frame_too_large and the connection is dropped.

Client -> Hub:  hello, say, state, roster, cmd, bye, pong
Hub -> Client:  welcome, deliver, ok, err, event, ping, roster
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PROTO_VERSION = 1

SAY_KINDS = frozenset(
    {"claim", "question", "answer", "result", "objection", "done", "note"}
)
STATES = frozenset({"idle", "busy", "blocked"})
COMMANDS = frozenset({"freeze", "resume", "reset", "mute", "unmute", "goal"})

ERR_PROTO_MISMATCH = "proto_mismatch"
ERR_NAME_TAKEN = "name_taken"
ERR_UNKNOWN_RECIPIENT = "unknown_recipient"
ERR_RATE_LIMITED = "rate_limited"
ERR_DUPLICATE = "duplicate"
ERR_FROZEN = "frozen"
ERR_MUTED = "muted"
ERR_FRAME_TOO_LARGE = "frame_too_large"
ERR_MALFORMED = "malformed"
ERR_FORBIDDEN = "forbidden"

RESERVED_NAMES = frozenset({"*", "hub", "system"})
SYSTEM_SENDER = "system"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
MAX_NAME_LEN = 32  # keep in sync with NAME_RE

MAX_ROLE_LEN = 256
MAX_DETAIL_LEN = 1024
MAX_GOAL_LEN = 1024
# C0/C1 controls except TAB (role/detail) resp. TAB/LF/CR (text)
_CTRL_LABEL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")
_CTRL_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


@dataclass
class ValidationError(Exception):
    code: str
    detail: str
    seq: int | None = None


def encode(frame: dict[str, Any]) -> bytes:
    """Serialize one frame as a compact NDJSON line."""
    return (json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _req_str(frame: dict[str, Any], key: str) -> str:
    val = frame.get(key)
    if not isinstance(val, str):
        raise ValidationError(ERR_MALFORMED, f"missing or non-string field: {key}")
    return val


def _req_int(frame: dict[str, Any], key: str) -> int:
    val = frame.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValidationError(ERR_MALFORMED, f"missing or non-int field: {key}")
    return val


def _opt_str(
    frame: dict[str, Any], key: str, max_len: int, ctrl: re.Pattern[str]
) -> None:
    val = frame.get(key)
    if val is None:
        return
    if not isinstance(val, str):
        raise ValidationError(ERR_MALFORMED, f"{key} must be a string")
    if len(val) > max_len:
        raise ValidationError(ERR_MALFORMED, f"{key} exceeds {max_len} characters")
    if ctrl.search(val):
        raise ValidationError(ERR_MALFORMED, f"{key} contains control characters")


def control_char_pos(text: str) -> int | None:
    """Index of the first character `say` text may not contain (C0/C1 except
    tab, LF, CR), or None."""
    m = _CTRL_TEXT.search(text)
    return m.start() if m else None


def validate_hello(frame: dict[str, Any]) -> None:
    proto = _req_int(frame, "proto")
    if proto != PROTO_VERSION:
        raise ValidationError(ERR_PROTO_MISMATCH, f"hub speaks proto={PROTO_VERSION}")
    name = _req_str(frame, "name")
    if name.casefold() in RESERVED_NAMES or not NAME_RE.match(name):
        raise ValidationError(ERR_MALFORMED, f"invalid name: {name!r}")
    kind = _req_str(frame, "kind")
    if not NAME_RE.match(kind):
        raise ValidationError(ERR_MALFORMED, f"invalid kind: {kind!r}")
    # room: reserved for v1.1 routing. Validated and logged, not used.
    room = frame.get("room")
    if room is not None and (not isinstance(room, str) or not NAME_RE.match(room)):
        raise ValidationError(ERR_MALFORMED, f"invalid room: {room!r}")
    caps = frame.get("caps", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise ValidationError(ERR_MALFORMED, "caps must be a list of strings")
    _opt_str(frame, "role", MAX_ROLE_LEN, _CTRL_LABEL)


def validate_say(frame: dict[str, Any]) -> None:
    to = _req_str(frame, "to")
    if not to:
        raise ValidationError(ERR_MALFORMED, "empty recipient")
    kind = _req_str(frame, "kind")
    if kind not in SAY_KINDS:
        raise ValidationError(ERR_MALFORMED, f"unknown say kind: {kind!r}")
    text = _req_str(frame, "text")
    if control_char_pos(text) is not None:
        raise ValidationError(
            ERR_MALFORMED,
            "text contains control characters (only \\t \\n \\r allowed)",
        )
    _req_int(frame, "seq")
    private = frame.get("private", False)
    if not isinstance(private, bool):
        raise ValidationError(ERR_MALFORMED, "private must be a boolean")
    if private and to == "*":
        raise ValidationError(ERR_MALFORMED, "a private say needs a named recipient")


def validate_state(frame: dict[str, Any]) -> None:
    state = _req_str(frame, "state")
    if state not in STATES:
        raise ValidationError(ERR_MALFORMED, f"unknown state: {state!r}")
    _opt_str(frame, "detail", MAX_DETAIL_LEN, _CTRL_LABEL)


def validate_cmd(frame: dict[str, Any]) -> None:
    cmd = _req_str(frame, "cmd")
    if cmd not in COMMANDS:
        raise ValidationError(ERR_MALFORMED, f"unknown command: {cmd!r}")
    args = frame.get("args")
    if args is not None and not isinstance(args, dict):
        raise ValidationError(ERR_MALFORMED, "args must be an object")
    if cmd == "resume" and isinstance(args, dict) and "n" in args:
        n = args["n"]
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            raise ValidationError(ERR_MALFORMED, "args.n must be a positive integer")
    if cmd == "goal":
        goal_args: dict[str, Any] = args if isinstance(args, dict) else {}
        text = goal_args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError(ERR_MALFORMED, "goal needs a non-empty args.text")
        _opt_str(goal_args, "text", MAX_GOAL_LEN, _CTRL_LABEL)
    seq = frame.get("seq")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)):
        raise ValidationError(ERR_MALFORMED, "seq must be an integer")


def opt_seq(frame: dict[str, Any]) -> int | None:
    val = frame.get("seq")
    return val if isinstance(val, int) and not isinstance(val, bool) else None


def validate(frame: dict[str, Any]) -> None:
    """Validate an inbound frame. Raises ValidationError (with the frame's
    seq attached when it carries a usable one)."""
    try:
        _validate(frame)
    except ValidationError as e:
        e.seq = opt_seq(frame)
        raise


def _validate(frame: dict[str, Any]) -> None:
    t = frame.get("t")
    if not isinstance(t, str):
        raise ValidationError(ERR_MALFORMED, "missing or non-string field: t")

    # Unknown frame types are a protocol violation and reported as malformed
    # (there is deliberately no separate code; spokes must answer ping, so a
    # well-formed ping can never trip this).
    def _noop(frame: dict[str, Any]) -> None:
        pass

    validators: dict[str, Callable[[dict[str, Any]], None]] = {
        "hello": validate_hello,
        "say": validate_say,
        "state": validate_state,
        "cmd": validate_cmd,
        "roster": _noop,
        "bye": _noop,
        "pong": _noop,
    }
    validator = validators.get(t)
    if validator is None:
        raise ValidationError(ERR_MALFORMED, f"unknown frame type: {t!r}")
    validator(frame)


def parse_line(line: bytes) -> dict[str, Any]:
    """Decode one NDJSON line. Raises ValidationError on bad input."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValidationError(ERR_MALFORMED, "frame is not valid UTF-8") from e
    try:
        frame = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValidationError(ERR_MALFORMED, f"invalid JSON: {e}") from e
    if not isinstance(frame, dict):
        raise ValidationError(ERR_MALFORMED, "frame is not a JSON object")
    return frame


def suggest_name(taken: str, is_free: Callable[[str], bool]) -> str:
    """Suggest '<name>-2', '<name>-3', ... — always a valid name, even when
    the stem is at the length limit (it is truncated to make room)."""
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = f"{taken[: MAX_NAME_LEN - len(suffix)]}{suffix}"
        if is_free(candidate):
            return candidate
        n += 1
