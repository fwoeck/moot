"""Hub state machine. Pure asyncio-free logic: the server layer feeds
frames in via handle() and the hub writes frames out through Client objects.

Floor control (see docs/PROTOCOL.md "Floor control"): every agent sees every
public message, but only addressed messages (direct/broadcast) trigger a
delivery; overheard messages ride along as context on the next wake. A private
say reaches its addressee and the observers only — no context copy, no backlog
copy. Round counting (see docs/PROTOCOL.md "Rounds, freeze, rate limit,
dedup"): +1 per say that wakes at least one agent, +1 per queued delivery fired
on an idle transition.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from moot.core import proto
from moot.core.bucket import TokenBucket
from moot.core.buffer import ContextBuffer
from moot.core.clock import Clock
from moot.core.config import Config
from moot.core.dedup import DedupWindow
from moot.core.notify import Notifier
from moot.core.transcript import Transcript
from moot.core.types import Addressing, Client, Msg, State

logger = logging.getLogger("moot.hub")

OBSERVER = "observer"
GOAL_FILE = "goal"


def write_goal(home: Path, text: str) -> None:
    """Persist the session goal at <home>/goal, mode 0600 — the durable copy
    a spoke reads through `moot brief` after a context compaction."""
    path = home / GOAL_FILE
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, f"{text}\n".encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def clear_goal(home: Path) -> None:
    (home / GOAL_FILE).unlink(missing_ok=True)


@dataclass
class Participant:
    name: str
    kind: str
    role: str
    caps: set[str]
    client: Client
    context: ContextBuffer
    bucket: TokenBucket
    dedup: DedupWindow
    state: State = "idle"
    queue: list[Msg] = field(default_factory=list)
    queue_dropped: int = 0
    finished: bool = False
    last_rx: float = 0.0
    busy_since: float = 0.0  # when it last became non-idle (inference timer)
    last_say: float = 0.0  # monotonic time of the last accepted say (quorum)
    blocked_since: float | None = None
    blocked_detail: str | None = None
    awaiting_pong: bool = False
    pong_deadline: float = 0.0

    @property
    def is_observer(self) -> bool:
        return self.kind == OBSERVER


class Hub:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        transcript: Transcript,
        notifier: Notifier,
    ) -> None:
        self.config = config
        self.clock = clock
        self.transcript = transcript
        self.notifier = notifier
        self.participants: dict[str, Participant] = {}
        self.dead: dict[str, Participant] = {}
        self.round = 0
        self.frozen = False
        self.muted = False
        self.goal: str = ""
        self.max_rounds = config.max_rounds
        self._last_activity = clock.monotonic()
        self._stall_latched = False
        # wall clock of the last session_done; cleared by the next agent say
        self._session_done_at: float | None = None
        # quorum: when an observer last addressed agents, whom, and whether
        # the event for that say has fired
        self._last_observer_say: float | None = None
        self._quorum_expected: set[str] = set()
        self._quorum_latched = False
        self._blocked_notified: set[str] = set()
        # Restart recovery: reseed the recent window and the id counter from
        # the transcript, so message ids stay unique across hub restarts.
        recent = self.transcript.recent_msgs()
        self._msg_counter = max(
            (int(r["id"]) for r in recent if isinstance(r.get("id"), int)), default=0
        )
        self._recent: list[dict[str, Any]] = recent[-config.recent_window :]

    # ------------------------------------------------------------------ util

    def _next_id(self) -> int:
        self._msg_counter += 1
        return self._msg_counter

    def _roster_detail(self) -> str:
        return ", ".join(sorted(self.participants)) or "(empty)"

    def _idle_for(self, p: Participant) -> float:
        return max(0.0, self.clock.monotonic() - p.last_rx)

    async def _send(self, p: Participant, frame: dict[str, object]) -> None:
        await p.client.send(frame)

    async def _err(
        self,
        p: Participant,
        code: str,
        detail: str,
        seq: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        frame: dict[str, object] = {
            "t": "err",
            "code": code,
            "detail": detail,
            "seq": seq,
        }
        if retry_after is not None:
            frame["retry_after"] = round(retry_after, 3)
        await self._send(p, frame)

    async def _emit_event(
        self, event: str, *, skip: Participant | None = None, **extra: object
    ) -> None:
        """Events go to observers only (see docs/PROTOCOL.md "event")."""
        frame: dict[str, object] = {"t": "event", "event": event, **extra}
        for p in list(self.participants.values()):
            if p.is_observer and p is not skip and p.client.connected:
                await self._send(p, frame)
        self.transcript.append(
            {"type": "event", "event": event, "ts": self.clock.wall(), **extra}
        )

    async def _emit_rejected(
        self,
        p: "Participant",
        code: str,
        to: str,
        kind: str,
        text: str,
        retry_after: float | None = None,
        private: bool = False,
    ) -> None:
        """Observer-visible record of a say the hub refused. Never carries the
        text — only its size, and whether it was private — and never fires for
        an observer's own say."""
        if p.is_observer:
            return
        extra: dict[str, Any] = {}  # event kwargs, not a frame
        if retry_after is not None:
            extra["retry_after"] = round(retry_after, 3)
        if private:
            extra["private"] = True
        await self._emit_event(
            "rejected",
            name=p.name,
            code=code,
            to=to,
            kind=kind,
            bytes=len(text.encode("utf-8")),
            **extra,
        )

    async def _notify(self, title: str, message: str) -> None:
        if self.config.notifications:
            await self.notifier.notify(title, message)

    # -------------------------------------------------------------- delivery

    def _enqueue(self, p: Participant, msg: Msg) -> None:
        """Append to the bounded wake queue, dropping the oldest on overflow."""
        if len(p.queue) >= self.config.queue_cap:
            del p.queue[0]
            p.queue_dropped += 1
        p.queue.append(msg)

    async def _deliver(self, p: Participant, msgs: list[Msg]) -> None:
        """Send one coalesced deliver frame. Caller guarantees at least one
        wake message is present (a deliver to an agent never carries overheard
        messages alone) and p.client.connected."""
        msgs = sorted(msgs, key=lambda m: m.id)
        frame: dict[str, object] = {
            "t": "deliver",
            "round": self.round,
            "msgs": [m.to_wire() for m in msgs],
        }
        await self._send(p, frame)
        self.transcript.append(
            {
                "type": "deliver",
                "to": p.name,
                "round": self.round,
                "msg_ids": [m.id for m in msgs],
                "overheard": [m.id for m in msgs if m.addressing == "overheard"],
                "ts": self.clock.wall(),
            }
        )
        if not p.is_observer:
            p.state = "busy"
            p.busy_since = self.clock.monotonic()
            self._touch_activity()

    async def _flush_queue(self, p: Participant) -> bool:
        """Idle transition with pending queue: one coalesced wake delivery.
        Counts as one round (see docs/PROTOCOL.md "Rounds, freeze, rate limit,
        dedup") unless the wake slice is system-only."""
        if not p.queue or not p.client.connected:
            return False
        if p.queue_dropped:
            p.queue.insert(
                0,
                Msg(
                    id=0,
                    sender=proto.SYSTEM_SENDER,
                    to=p.name,
                    kind="note",
                    text=(
                        f"{p.queue_dropped} addressed messages dropped "
                        "while you were busy"
                    ),
                    ts=0.0,
                    addressing="direct",
                ),
            )
            p.queue_dropped = 0
        wake = p.queue[: self.config.deliver_wake_cap]
        p.queue = p.queue[self.config.deliver_wake_cap :]
        # Bound the context drain by id only while older wakes remain queued;
        # the slice that empties the queue drains everything (context newer
        # than the last wake rides along, as today) — otherwise it would be
        # stranded until the next unrelated addressed message.
        upto = wake[-1].id if p.queue else None
        msgs = p.context.drain(upto_id=upto) + wake
        if any(m.sender != proto.SYSTEM_SENDER for m in wake):
            self.round += 1
            await self._deliver(p, msgs)
            await self._check_round_limit()
        else:
            await self._deliver(p, msgs)
        await self._check_session_done()
        return True

    async def _system_message(self, text: str) -> None:
        """Agent-directed system message (see docs/PROTOCOL.md "System
        messages"): a deliver from the reserved sender 'system' with addressing
        direct. Wakes idle agents, queues for busy ones. Not round-counted
        (control plane, human-driven)."""
        for p in list(self.participants.values()):
            if p.is_observer:
                continue
            msg = Msg(
                id=self._next_id(),
                sender=proto.SYSTEM_SENDER,
                to=p.name,
                kind="note",
                text=text,
                ts=self.clock.wall(),
                addressing="direct",
            )
            if p.state == "idle" and p.client.connected:
                await self._deliver(p, [*p.context.drain(upto_id=msg.id), msg])
            else:
                self._enqueue(p, msg)

    # ----------------------------------------------------------------- hello

    async def handle_hello(
        self, client: Client, frame: dict[str, object]
    ) -> Participant | None:
        name = str(frame["name"])
        if client.name:
            await self._err_raw(
                client, proto.ERR_MALFORMED, f"already registered as {client.name!r}"
            )
            return None
        old = self.participants.get(name)
        if old is not None and not old.client.connected:
            # The previous holder's transport is already gone but its read loop
            # has not reaped it yet: reap now so this hello reclaims instead of
            # getting name_taken.
            await self.disconnect(old.client)
        kind = str(frame["kind"])
        role = str(frame.get("role") or "")
        caps_raw = frame.get("caps", [])
        caps = {str(c) for c in caps_raw} if isinstance(caps_raw, list) else set()
        room = frame.get("room")
        if isinstance(room, str):
            self.transcript.append(
                {
                    "type": "event",
                    "event": "room_declared",
                    "room": room,
                    "name": name,
                    "ts": self.clock.wall(),
                }
            )

        if name in self.participants:
            suggestion = proto.suggest_name(
                name, lambda n: n not in self.participants and n not in self.dead
            )
            await self._err_raw(client, proto.ERR_NAME_TAKEN, suggestion)
            await client.close()
            return None

        now = self.clock.monotonic()
        if name in self.dead:
            # Reclaim (see docs/PROTOCOL.md "hello"): inherit queue and context
            # buffer of the dead connection. This is also the restart-reconnect
            # path.
            p = self.dead.pop(name)
            p.client = client
            p.kind = kind
            p.role = role
            p.caps = caps
            p.state = "idle"
            p.last_rx = now
            p.awaiting_pong = False
            p.finished = False  # the new process has declared nothing yet
            p.blocked_since = None
            p.blocked_detail = None
            self._blocked_notified.discard(name)
            rejoined = True
        else:
            p = Participant(
                name=name,
                kind=kind,
                role=role,
                caps=caps,
                client=client,
                context=ContextBuffer(self.config.context_cap),
                bucket=TokenBucket(
                    self.clock,
                    self.config.rate_burst,
                    self.config.rate_messages / self.config.rate_window,
                ),
                dedup=DedupWindow(self.clock, self.config.dedup_window),
                last_rx=now,
            )
            rejoined = False

        self.participants[name] = p
        client.name = name
        await self._send(
            p,
            {
                "t": "welcome",
                "name": name,
                "round": self.round,
                "peers": [
                    {"name": q.name, "kind": q.kind, "role": q.role, "state": q.state}
                    for q in self.participants.values()
                    if q is not p
                ],
                "limits": {
                    "rate": (
                        f"{self.config.rate_messages}/{int(self.config.rate_window)}s"
                    ),
                    "max_rounds": self.max_rounds,
                },
                **({"goal": self.goal} if self.goal else {}),
            },
        )
        # Late join (see docs/PROTOCOL.md "hello"): an agent gets the backlog as
        # overheard context, never a wake; an observer gets it delivered — its
        # buffer is never drained. A reclaiming observer (restarted operator
        # TUI) needs it too.
        if not rejoined or p.is_observer:
            # A private message belongs to its addressee and the observers:
            # filtered before the slice, so it never costs a joiner a slot.
            window = [
                rec
                for rec in self._recent
                if p.is_observer or not rec.get("private") or rec.get("to") == name
            ]
            backlog = [
                self._msg_from_record(rec)
                for rec in window[-self.config.backlog_on_join :]
                if rec.get("from") != name
            ]
            if p.is_observer:
                if backlog:
                    await self._deliver(
                        p, [m.tag(self._addressing(p, m.to)) for m in backlog]
                    )
            else:
                for m in backlog:
                    p.context.append(m.tag("overheard"))
        self._touch_activity()
        await self._emit_event("peer_joined", name=name, kind=kind, role=role, skip=p)
        if rejoined and p.queue:
            await self._flush_queue(p)
        return p

    def _msg_from_record(self, rec: dict[str, Any]) -> Msg:
        return Msg(
            id=int(rec["id"]) if isinstance(rec.get("id"), int) else 0,
            sender=str(rec.get("from", "?")),
            to=str(rec.get("to", "*")),
            kind=str(rec.get("kind", "note")),
            text=str(rec.get("text", "")),
            ts=float(rec["ts"]) if isinstance(rec.get("ts"), (int, float)) else 0.0,
            private=bool(rec.get("private")),
        )

    # ------------------------------------------------------------------- say

    def _addressing(self, recipient: Participant, to: str) -> Addressing:
        if to == recipient.name:
            return "direct"
        if to == "*":
            return "broadcast"
        return "overheard"

    def _set_idle(self, p: Participant) -> None:
        p.state = "idle"
        p.blocked_since = None
        p.blocked_detail = None

    async def handle_say(self, p: Participant, frame: dict[str, object]) -> None:
        accepted = await self._route_say(p, frame)
        # A say *frame* from a capability-less participant proves it is
        # producing output (PROTOCOL: "when their say arrives: idle"),
        # whether or not the hub accepted the message.
        if "idle-events" not in p.caps and p.state != "idle":
            self._set_idle(p)
            await self._flush_queue(p)
        if accepted and not p.is_observer:
            if frame["kind"] == "done":
                await self._mark_done(p)
            else:
                p.finished = False  # a non-done say proves continued work

    async def _route_say(self, p: Participant, frame: dict[str, object]) -> bool:
        """Checks in PROTOCOL order (frozen, muted, recipient, rate, dedup),
        then routing. Returns True iff the say was accepted. A say rejected
        before the rate check consumes no token, and no rejected say enters
        the dedup window — a duplicate has already spent its token."""
        to = str(frame["to"])
        kind = str(frame["kind"])
        text = str(frame["text"])
        seq = cast(int, frame["seq"])  # validate_say guarantees an int
        private = bool(frame.get("private", False))

        if not p.is_observer:
            if self.frozen:
                await self._err(
                    p, proto.ERR_FROZEN, "hub is frozen — waiting for resume", seq
                )
                await self._emit_rejected(
                    p, proto.ERR_FROZEN, to, kind, text, private=private
                )
                return False
            if self.muted and (to == "*" or self._targets_agent(to)):
                await self._err(
                    p,
                    proto.ERR_MUTED,
                    "mute mode active (broadcast and agent→agent blocked; "
                    "direct to observers allowed)",
                    seq,
                )
                await self._emit_rejected(
                    p, proto.ERR_MUTED, to, kind, text, private=private
                )
                return False
        if to != "*" and to not in self.participants:
            await self._err(
                p,
                proto.ERR_UNKNOWN_RECIPIENT,
                f"unknown recipient {to!r} — roster: {self._roster_detail()}",
                seq,
            )
            await self._emit_rejected(
                p, proto.ERR_UNKNOWN_RECIPIENT, to, kind, text, private=private
            )
            return False
        if not p.is_observer:
            if not p.bucket.take():
                retry = p.bucket.retry_after()
                await self._err(
                    p,
                    proto.ERR_RATE_LIMITED,
                    "rate limit",
                    seq,
                    retry_after=retry,
                )
                await self._emit_rejected(
                    p,
                    proto.ERR_RATE_LIMITED,
                    to,
                    kind,
                    text,
                    retry_after=retry,
                    private=private,
                )
                return False
            if p.dedup.seen(to, text, private):
                await self._err(
                    p,
                    proto.ERR_DUPLICATE,
                    "identical message within dedup window",
                    seq,
                )
                await self._emit_rejected(
                    p, proto.ERR_DUPLICATE, to, kind, text, private=private
                )
                return False

        msg = Msg(
            id=self._next_id(),
            sender=p.name,
            to=to,
            kind=kind,
            text=text,
            ts=self.clock.wall(),
            seq=seq,
            private=private,
        )
        p.last_say = self.clock.monotonic()
        if p.is_observer:
            if to == "*":
                expected = {
                    q.name
                    for q in self.participants.values()
                    if not q.is_observer and q.client.connected
                }
            elif self._targets_agent(to):
                expected = {to}
            else:
                expected = set()  # observer → observer: no agent is asked anything
            if expected:
                self._last_observer_say = p.last_say
                self._quorum_expected = expected
                self._quorum_latched = False
        else:
            self._session_done_at = None
        self.transcript.append(
            {
                "type": "msg",
                "id": msg.id,
                "round": self.round,
                "from": p.name,
                "to": to,
                "kind": kind,
                "text": text,
                "ts": msg.ts,
                **({"private": True} if private else {}),
            }
        )
        self._recent.append(
            {
                "id": msg.id,
                "from": p.name,
                "to": to,
                "kind": kind,
                "text": text,
                "ts": msg.ts,
                **({"private": True} if private else {}),
            }
        )
        del self._recent[: -self.config.recent_window]  # cap the in-memory window

        queued_for = 0
        woken: list[tuple[Participant, list[Msg]]] = []
        observers: list[tuple[Participant, list[Msg]]] = []
        for q in list(self.participants.values()):
            if q is p:
                continue  # never route back to the sender, even on broadcast
            mode = self._addressing(q, to)
            tagged = msg.tag(mode)
            if q.is_observer:
                if q.client.connected:
                    observers.append((q, [tagged]))
            elif mode == "overheard":
                if not msg.private:  # a private say has no third-party copy
                    q.context.append(tagged)
            elif q.state == "idle" and q.client.connected:
                woken.append((q, [*q.context.drain(upto_id=msg.id), tagged]))
            else:
                self._enqueue(q, tagged)
                queued_for += 1

        if woken:
            # One round per wake-causing say, independent of recipient count.
            self.round += 1
        # Observers are delivered after the increment so their frame carries
        # the same round number as the wake it accompanies.
        for q, batch in observers:
            await self._deliver(q, batch)
        for q, batch in woken:
            await self._deliver(q, batch)
        if woken:
            await self._check_round_limit()

        await self._send(p, {"t": "ok", "id": msg.id, "queued": queued_for, "seq": seq})
        if not p.is_observer and (
            (self._targets_observer(to) and not self._observer_present(to))
            or (to == "*" and kind == "question" and not self._observer_present())
        ):
            head = " ".join(text.split())[:80]
            await self._notify("moot", f"{p.name} \u2192 {to} \u00b7 {kind}: {head}")
        self._touch_activity()
        return True

    def _targets_agent(self, to: str) -> bool:
        q = self.participants.get(to)
        return q is not None and not q.is_observer

    def _targets_observer(self, to: str) -> bool:
        q = self.participants.get(to)
        return q is not None and q.is_observer

    def _observer_present(self, name: str | None = None) -> bool:
        """An observer is present while its last inbound frame is younger
        than `present_within`: the TUI polls the roster every 3 s, a classic
        seat only sends when the human types. `name` restricts the question
        to one seat; without it any connected observer counts."""
        now = self.clock.monotonic()
        return any(
            now - q.last_rx < self.config.present_within
            for q in self.participants.values()
            if q.is_observer and q.client.connected and (name is None or q.name == name)
        )

    async def _mark_done(self, p: Participant) -> None:
        p.finished = True
        await self._check_session_done()

    async def _check_session_done(self) -> None:
        """All connected agents finished *and* every agent queue drained →
        session_done, round reset, marks cleared. Called after a done say,
        after a queue flush, and after a disconnect."""
        agents = [q for q in self.participants.values() if not q.is_observer]
        if (
            agents
            and all(q.finished for q in agents)
            and all(not q.queue for q in agents)
        ):
            await self._emit_event("session_done", round=self.round)
            self._session_done_at = self.clock.wall()
            await self._notify("moot", "session_done — all agents finished")
            self.round = 0
            for q in agents:
                q.finished = False

    async def _check_round_limit(self) -> None:
        if self.round >= self.max_rounds and not self.frozen:
            self.frozen = True
            await self._emit_event(
                "round_limit", round=self.round, max_rounds=self.max_rounds
            )
            await self._notify(
                "moot", f"round_limit reached (round {self.round}) — frozen"
            )
            if not any(q.is_observer for q in self.participants.values()):
                # Freeze without an observer cannot be lifted.
                logger.warning(
                    "round limit reached with no observer connected — "
                    "nobody can thaw the hub"
                )
                self.transcript.append(
                    {
                        "type": "event",
                        "event": "freeze_without_observer",
                        "ts": self.clock.wall(),
                    }
                )

    # ----------------------------------------------------------------- state

    async def handle_state(self, p: Participant, frame: dict[str, object]) -> None:
        state = cast(State, frame["state"])  # validate_state guarantees membership
        detail = cast(str | None, frame.get("detail"))
        prev = p.state
        p.state = state
        if state != "idle":
            p.busy_since = self.clock.monotonic()
        if state == "blocked":
            # Only a *transition* (or a changed detail) rearms the watchdog:
            # a blocked heartbeat must not restart the timer.
            if prev != "blocked" or p.blocked_detail != detail:
                p.blocked_since = self.clock.monotonic()
                p.blocked_detail = detail
                self._blocked_notified.discard(p.name)
        else:
            p.blocked_since = None
            p.blocked_detail = None
            if state == "idle":
                await self._flush_queue(p)

    # ------------------------------------------------------------------- cmd

    async def handle_cmd(self, p: Participant, frame: dict[str, object]) -> None:
        seq = cast(int | None, frame.get("seq"))
        if not p.is_observer:
            await self._err(p, proto.ERR_FORBIDDEN, "cmd is observer-only", seq)
            return
        cmd = str(frame["cmd"])
        args_raw = frame.get("args")
        args: dict[str, Any] = args_raw if isinstance(args_raw, dict) else {}

        if cmd == "freeze":
            self.frozen = True
            await self._emit_event("frozen", round=self.round)
        elif cmd == "resume":
            n = args.get("n")  # validated positive int or absent (validate_cmd)
            if isinstance(n, int):
                self.max_rounds = max(self.max_rounds, self.round) + n
            elif self.round >= self.max_rounds:
                # Bare resume after a *limit* freeze grants a fresh budget;
                # after a manual freeze it leaves the limit alone.
                self.max_rounds = self.round + self.config.max_rounds
            self.frozen = False
            await self._emit_event(
                "resumed", round=self.round, max_rounds=self.max_rounds
            )
        elif cmd == "reset":
            from_round = self.round
            self.round = 0
            self.frozen = False
            await self._emit_event(
                "reset", round=0, from_round=from_round, max_rounds=self.max_rounds
            )
        elif cmd == "mute":
            self.muted = True
            await self._emit_event("muted")
            await self._system_message(
                "moot: mute mode active — say to other agents is rejected (err: muted)"
            )
        elif cmd == "goal":
            # validate_cmd guarantees a non-empty string.
            self.goal = str(args["text"])
            await self._emit_event("goal_set", text=self.goal)
            self._buffer_goal(self.goal)
            write_goal(self.config.home, self.goal)
        elif cmd == "unmute":
            self.muted = False
            await self._emit_event("unmuted")
            await self._system_message(
                "moot: mute mode lifted — agent→agent allowed again"
            )
        self._touch_activity()
        await self._send(p, {"t": "ok", "seq": seq})

    def _buffer_goal(self, text: str) -> None:
        """The goal rides along as overheard context on every agent's next
        wake. Like the placeholder lines it carries `id: 0`: it is not a
        citable message and never a transcript `msg`, so it must not consume
        an id (a consumed id would be a gap in the transcript and a dangling
        reference in the `deliver` record). The line can be evicted from the
        context buffer; `<home>/goal` and `moot brief` are the durable path."""
        msg = Msg(
            id=0,
            sender=proto.SYSTEM_SENDER,
            to="*",
            kind="note",
            text=f"moot goal: {text}",
            ts=self.clock.wall(),
        )
        for q in self.participants.values():
            if not q.is_observer:
                q.context.append(msg.tag("overheard"))

    # ---------------------------------------------------------------- roster

    async def handle_roster(self, p: Participant, frame: dict[str, object]) -> None:
        await self._send(
            p,
            {
                "t": "roster",
                "round": self.round,
                "max_rounds": self.max_rounds,
                "frozen": self.frozen,
                "muted": self.muted,
                "seq": proto.opt_seq(frame),
                **({"goal": self.goal} if self.goal else {}),
                "peers": [
                    {
                        "name": q.name,
                        "kind": q.kind,
                        "role": q.role,
                        "state": q.state,
                        "idle_for": round(self._idle_for(q), 1),
                        "busy_for": self._busy_for(q),
                        "finished": q.finished,
                        "queued": len(q.queue),
                        "queued_from": sorted({m.sender for m in q.queue}),
                        "dropped": q.queue_dropped,
                        "context": len(q.context),
                        "blocked_detail": q.blocked_detail,
                    }
                    for q in self.participants.values()
                ],
            },
        )

    def _busy_for(self, q: Participant) -> float:
        """Seconds since the participant last became non-idle; 0.0 while idle."""
        if q.state == "idle":
            return 0.0
        return round(max(0.0, self.clock.monotonic() - q.busy_since), 1)

    # --------------------------------------------------------------- watchdog

    async def watchdog_tick(self) -> None:
        now = self.clock.monotonic()
        for name, p in list(self.dead.items()):
            if now - p.last_rx > self.config.dead_ttl:
                del self.dead[name]
        for p in list(self.participants.values()):
            if not p.client.connected:
                # Transport already gone but the read loop has not reaped it:
                # do it here, so the name does not linger as undeliverable.
                await self.disconnect(p.client)
                continue
            # Dead connection detection (see docs/PROTOCOL.md "ping").
            silence = now - p.last_rx
            if p.awaiting_pong and now > p.pong_deadline:
                await self.disconnect(p.client)
                continue
            if silence > self.config.dead_after and not p.awaiting_pong:
                p.awaiting_pong = True
                p.pong_deadline = now + self.config.pong_timeout
                await self._send(p, {"t": "ping"})

            if p.is_observer:
                continue
            # State inference timeout for capability-less participants (see
            # docs/PROTOCOL.md "State inference").
            # The assume-idle timer counts from the moment the participant
            # became non-idle (delivery or state report) and is reset only by
            # its next say — not by pongs or other frames, which prove the
            # spoke process is alive, not that the agent produced output.
            if (
                "idle-events" not in p.caps
                and p.state != "idle"
                and now - p.busy_since > self.config.assume_idle_after
            ):
                self._set_idle(p)
                await self._emit_event(
                    "stall",
                    detail=f"no sign of life from {p.name}, assumed idle",
                )
                await self._flush_queue(p)
            # Blocked notification (see docs/PROTOCOL.md "Watchdog behavior
            # clients observe").
            if (
                p.state == "blocked"
                and p.blocked_since is not None
                and now - p.blocked_since > self.config.blocked_after
                and p.name not in self._blocked_notified
            ):
                self._blocked_notified.add(p.name)
                await self._emit_event(
                    "blocked", name=p.name, detail=p.blocked_detail or ""
                )
                await self._notify(
                    "moot", f"{p.name} blocked: {p.blocked_detail or ''}"
                )

        agents = [q for q in self.participants.values() if not q.is_observer]
        # Quorum (see docs/PROTOCOL.md "Watchdog behavior clients observe"):
        # every agent the observer's last say addressed has answered.
        asked_at = self._last_observer_say  # local: no narrowing inside the generator
        if (
            agents
            and not self._quorum_latched
            and asked_at is not None
            and all(q.state == "idle" for q in agents)
            and all(not q.queue for q in agents)
        ):
            expected = [q for q in agents if q.name in self._quorum_expected]
            if expected and all(q.last_say > asked_at for q in expected):
                self._quorum_latched = True
                names = ", ".join(sorted(q.name for q in expected))
                await self._emit_event(
                    "quorum", detail=f"every addressed agent has answered — {names}"
                )
                if not self._observer_present():
                    await self._notify("moot", f"quorum — {names} answered")
        if (
            agents
            and all(q.state == "idle" for q in agents)
            and all(not q.queue for q in agents)
            and now - self._last_activity > self.config.stall_after
            and not self._stall_latched
        ):
            self._stall_latched = True
            await self._emit_event("stall", detail=self._stall_detail(agents))
            await self._notify("moot", "stall — all agents idle")

    # -------------------------------------------------------------- lifecycle

    async def handle(self, client: Client, frame: dict[str, object]) -> None:
        """Dispatch one inbound frame. Validation errors become err frames."""
        try:
            proto.validate(frame)
        except proto.ValidationError as e:
            await self._err_raw(client, e.code, e.detail, e.seq)
            return

        t = frame["t"]
        if t == "hello":
            await self.handle_hello(client, frame)
            return

        p = self.participants.get(client.name)
        if p is None:
            await self._err_raw(client, proto.ERR_MALFORMED, "hello expected first")
            return
        p.last_rx = self.clock.monotonic()
        if p.awaiting_pong:
            p.awaiting_pong = False  # any frame is a life sign; pong explicitly

        if t == "say":
            await self.handle_say(p, frame)
        elif t == "state":
            await self.handle_state(p, frame)
        elif t == "cmd":
            await self.handle_cmd(p, frame)
        elif t == "roster":
            await self.handle_roster(p, frame)
        elif t == "bye":
            await self.disconnect(client)
        elif t == "pong":
            pass
        else:
            raise RuntimeError(f"frame type {t!r} passed validation but has no handler")

    async def _err_raw(
        self, client: Client, code: str, detail: str, seq: int | None = None
    ) -> None:
        await client.send({"t": "err", "code": code, "detail": detail, "seq": seq})

    async def disconnect(self, client: Client) -> None:
        p = self.participants.get(client.name)
        if p is not None and p.client is client:
            # Registration survives as dead: reclaim keeps queue and context.
            self.dead[p.name] = p
            del self.participants[p.name]
            self._blocked_notified.discard(p.name)
            self._touch_activity()
            await self._emit_event("peer_left", name=p.name)
            await self._check_session_done()
        client.connected = False
        await client.close()

    def _touch_activity(self) -> None:
        """Conversation happened: feeds the stall detector."""
        self._last_activity = self.clock.monotonic()
        self._stall_latched = False

    def _stall_detail(self, agents: list[Participant]) -> str:
        """`unread context` is what the stall gate ignores on purpose: a
        buffered line nobody was woken for — an exchange between others this
        agent only overheard, or the `/goal` line. (A private say is never
        context: it wakes its addressee or waits in its queue, and a queue
        blocks the stall anyway.) The gate stays state+queue — see PROTOCOL."""
        unread = ", ".join(
            f"{q.name} {len(q.context)}"
            for q in sorted(agents, key=lambda q: q.name)
            if len(q.context)
        )
        head = f"all agents idle, nothing queued — unread context: {unread or '—'}"
        if self._session_done_at is not None:
            stamp = time.strftime("%H:%M:%S", time.localtime(self._session_done_at))
            return f"{head}; session done at {stamp}"
        done = sorted(q.name for q in agents if q.finished)
        not_done = sorted(q.name for q in agents if not q.finished)
        return (
            f"{head}; done: {', '.join(done) or '—'}; "
            f"not done: {', '.join(not_done) or '—'}"
        )


def new_hub(
    config: Config, clock: Clock | None = None, notifier: Notifier | None = None
) -> Hub:
    """Production constructor: real clock, transcript on disk."""
    from moot.core.notify import NullNotifier

    clock = clock or Clock()
    transcript = Transcript(config.transcript_dir, clock)
    return Hub(config, clock, transcript, notifier or NullNotifier())
