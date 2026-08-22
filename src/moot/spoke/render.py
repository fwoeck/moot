"""Inbound frames as text for a model.

The line shapes are the contract between the Python spokes and the OpenCode
plugin, pinned by tests/fixtures/render_cases.json — change both or neither.
Message text is emitted verbatim, so a rendered line may span several lines.
A private message carries a `· private` marker between addressee and kind —
part of the pinned shape, because the recipient must be able to tell.
"""

from typing import Any


def _items(frame: dict[str, object], key: str) -> list[Any]:
    value = frame.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{frame.get('t')!r} frame without a {key} list")
    return value


def _int_id(value: object) -> int:
    """A usable message id, else 0 — bools and non-positive ints are not ids."""
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )


def message_head(round_: object, msg_id: int) -> str:
    """`[r3 #42]`, or `[context #42]` when `round_` is None. No id, no `#tag`."""
    label = "context" if round_ is None else f"r{round_}"
    return f"[{label} #{msg_id}]" if msg_id else f"[{label}]"


def message_line(
    round_: object,
    msg_id: int,
    sender: str,
    to: str,
    kind: str,
    text: str,
    *,
    private: bool = False,
) -> str:
    mark = " · private" if private else ""
    return f"{message_head(round_, msg_id)} {sender} → {to}{mark} · {kind}: {text}"


def _deliver(frame: dict[str, object]) -> list[str]:
    out = []
    for m in _items(frame, "msgs"):
        overheard = m["addressing"] == "overheard"
        out.append(
            message_line(
                None if overheard else frame["round"],
                _int_id(m.get("id")),
                m["from"],
                m["to"],
                m["kind"],
                m["text"],
                private=m.get("private") is True,
            )
        )
    return out


def _peer(p: Any) -> str:
    # A role belongs to an agent; the observer's is not floor information.
    role = p.get("role")
    if p["kind"] != "observer" and isinstance(role, str) and role:
        return f"{p['name']} ({p['kind']}, {role}, {p['state']})"
    return f"{p['name']} ({p['kind']}, {p['state']})"


def _peers(frame: dict[str, object]) -> str:
    peers = [_peer(p) for p in _items(frame, "peers")]
    return ", ".join(peers) if peers else "none"


def _rounds(frame: dict[str, object]) -> str:
    """`7/24` when the hub sent a round limit, else `7`."""
    limits = frame.get("limits")
    max_rounds = limits.get("max_rounds") if isinstance(limits, dict) else None
    return (
        f"{frame['round']}" if max_rounds is None else f"{frame['round']}/{max_rounds}"
    )


def _welcome(frame: dict[str, object]) -> str:
    # The hub's welcome carries no kind for the joiner itself (PROTOCOL.md
    # "welcome"); `moot stream` knows it and adds it before rendering.
    kind = frame.get("kind")
    who = f"{frame['name']} ({kind})" if isinstance(kind, str) else f"{frame['name']}"
    return f"[moot] joined as {who} · peers: {_peers(frame)} · round {_rounds(frame)}"


def rejoined_line(frame: dict[str, object]) -> str:
    """The welcome of a reconnect: the floor may have moved on without us."""
    return (
        f"[moot] rejoined as {frame['name']} · round {_rounds(frame)}"
        f" · peers: {_peers(frame)}"
    )


def render(frame: dict[str, object]) -> list[str]:
    t = frame.get("t")
    if t == "deliver":
        return _deliver(frame)
    if t == "event":
        rest = " ".join(f"{k}={v}" for k, v in frame.items() if k not in ("t", "event"))
        head = f"[event] {frame['event']}"
        return [f"{head} · {rest}" if rest else head]
    if t == "err":
        line = f"[err] {frame['code']} · {frame.get('detail')}"
        retry_after = frame.get("retry_after")
        # the hub's rate-limit detail is a constant; the wait is only here
        return [line if retry_after is None else f"{line} · retry in {retry_after}s"]
    if t == "ok":
        return []  # confirmations are noise for the reader; errs are not
    if t == "roster":
        return [
            f"[roster] {p['name']} ({p['kind']}, {p['state']})"
            for p in _items(frame, "peers")
        ]
    if t == "welcome":
        return [_welcome(frame)]
    return []
