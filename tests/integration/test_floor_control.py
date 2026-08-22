"""Integration tests with fake spokes: floor control, rounds, lifecycle."""

import asyncio
import json
import logging
import time

import pytest

from moot.core import proto
from tests.conftest import FakeClient, Harness, YieldingClient


async def test_broadcast_reaches_agents_and_observer(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("alpha", "*", "befund")

    beta_msgs = harness.delivered("beta")
    assert [m["text"] for m in beta_msgs] == ["befund"]
    assert beta_msgs[0]["addressing"] == "broadcast"
    observer_msgs = harness.delivered("frank")
    assert [m["text"] for m in observer_msgs] == ["befund"]
    # the sender never gets their own broadcast back
    assert harness.delivered("alpha") == []


async def test_direct_message_only_wakes_addressee(harness: Harness):
    """frank → alpha with two idle agents: alpha gets one deliver, beta gets
    none — the message sits in beta's context buffer. Round is 1, not 2."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "alpha", "nur für dich")

    assert len(harness.clients["alpha"].frames("deliver")) == 1
    assert harness.delivered("alpha")[0]["addressing"] == "direct"
    assert harness.clients["beta"].frames("deliver") == []
    assert harness.hub.round == 1
    assert len(harness.hub.participants["beta"].context) == 1


async def test_deferred_delivery_carries_overheard_first(harness: Harness):
    """After frank → alpha, a frank → beta: beta's deliver has two entries,
    the buffered overheard first, then the direct one."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "alpha", "für alpha")
    await harness.say("frank", "beta", "für beta")

    msgs = harness.delivered("beta")
    assert [m["addressing"] for m in msgs] == ["overheard", "direct"]
    assert msgs[0]["text"] == "für alpha" and msgs[1]["text"] == "für beta"
    assert msgs[0]["to"] == "alpha"  # original addressee stays visible


async def test_buffer_without_wake_delivers_nothing(harness: Harness):
    """Ten messages between frank and alpha while beta is idle: beta receives
    zero deliveries."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    for i in range(10):
        await harness.say("frank", "alpha", f"m{i}")
        await harness.state("alpha", "idle")  # alpha answers, goes idle again
    assert harness.clients["beta"].frames("deliver") == []
    assert len(harness.hub.participants["beta"].context) == 10


async def test_busy_recipient_is_queued_then_coalesced(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("frank", "alpha", "erste")  # wakes alpha → busy
    harness.clients["alpha"].clear()
    await harness.say("frank", "alpha", "zweite")  # alpha busy → queued
    assert harness.clients["alpha"].frames("deliver") == []
    await harness.state("alpha", "idle")
    msgs = harness.delivered("alpha")
    assert [m["text"] for m in msgs] == ["zweite"]


async def test_ok_queued_counts_recipients_not_length(harness: Harness):
    """Broadcast to two busy agents → queued counts recipients, so 2."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "*", "weckt beide")  # both busy now
    harness.clients["frank"].clear()
    await harness.say("frank", "*", "beide busy")
    ok = harness.clients["frank"].last("ok")
    assert ok is not None and ok["queued"] == 2


async def test_sender_never_gets_own_broadcast(harness: Harness):
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("alpha", "*", "claim")
    for msg in harness.delivered("alpha"):
        assert msg["from"] != "alpha" or msg["to"] == "alpha"


async def test_observer_never_queued_even_when_frozen(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.hub.frozen = True
    await harness.say("alpha", "frank", "wird abgelehnt")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_FROZEN  # agents blocked
    await harness.say("frank", "alpha", "observer während freeze")  # observer exempt
    assert harness.delivered("alpha")[0]["text"] == "observer während freeze"


async def test_freeze_blocks_agent_say_and_resume_thaws(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.hub.frozen = True
    await harness.say("alpha", "frank", "blockiert")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_FROZEN
    await harness.cmd("frank", "resume", {"n": 12})
    await harness.say("alpha", "frank", "jetzt geht es")
    assert harness.delivered("frank")[-1]["text"] == "jetzt geht es"


async def test_cmd_forbidden_for_agents(harness: Harness):
    await harness.join("alpha")
    await harness.cmd("alpha", "mute")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_FORBIDDEN
    assert harness.hub.muted is False


async def test_round_limit_freezes_and_notifies(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    harness.hub.max_rounds = 2
    await harness.say("frank", "alpha", "runde 1")
    await harness.state("alpha", "idle")
    await harness.say("frank", "alpha", "runde 2")
    assert harness.hub.frozen is True
    events = [
        f
        for f in harness.clients["frank"].frames("event")
        if f["event"] == "round_limit"
    ]
    assert len(events) == 1
    assert (
        "moot",
        "round_limit reached (round 2) — frozen",
    ) in harness.notifier.calls


async def test_broadcast_counts_one_round(harness: Harness):
    """A broadcast waking two agents is one round, not two (see
    docs/PROTOCOL.md "Rounds, freeze, rate limit, dedup")."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "*", "briefing")
    assert harness.hub.round == 1


async def test_queued_delivery_on_idle_transition_counts_one_round(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("frank", "alpha", "wake")  # round 1, alpha busy
    await harness.say("frank", "alpha", "queued")  # alpha busy → queued
    assert harness.hub.round == 1
    await harness.state("alpha", "idle")  # flush → round 2
    assert harness.hub.round == 2


async def test_rate_limit_after_burst(harness: Harness):
    """Burst 3, refill 6/60s → the 4th immediate message is rejected with a
    plausible retry_after (one token per 10 s)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    for i in range(3):
        await harness.say("alpha", "frank", f"m{i}")
    await harness.say("alpha", "frank", "eine zu viel")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_RATE_LIMITED
    assert err["retry_after"] == pytest.approx(10.0)
    # after one refill window the next message passes again
    harness.clock.advance(10.0)
    harness.clients["alpha"].clear()
    await harness.say("alpha", "frank", "geht wieder")
    assert harness.clients["alpha"].last("err") is None


async def test_observer_exempt_from_rate_limit_and_dedup(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    for _ in range(8):
        await harness.say("frank", "alpha", "gleiche nachricht")
    errs = harness.clients["frank"].frames("err")
    assert not any(
        e["code"] in (proto.ERR_RATE_LIMITED, proto.ERR_DUPLICATE) for e in errs
    )


async def test_duplicate_rejected(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("alpha", "frank", "selbe")
    await harness.say("alpha", "frank", "selbe")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_DUPLICATE


async def test_unknown_recipient_carries_roster(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("alpha", "gamma", "an ein phantom")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_UNKNOWN_RECIPIENT
    assert "alpha" in err["detail"] and "frank" in err["detail"]


async def test_name_taken_suggests_alternative(harness: Harness):
    await harness.join("alpha")
    await harness.join("alpha")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_NAME_TAKEN
    assert err["detail"] == "alpha-2"


async def test_reconnect_reclaims_queue_and_context(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "alpha", "erste")  # wakes alpha → busy
    await harness.say("frank", "beta", "kontext für alpha")  # → alpha context
    await harness.say("frank", "alpha", "zweite")  # queued (busy)
    # alpha dies mid-queue; reconnect with same name reclaims queue AND context
    await harness.hub.disconnect(harness.clients["alpha"])
    client = await harness.join("alpha")
    msgs = [m for f in client.frames("deliver") for m in f["msgs"]]
    texts = [m["text"] for m in msgs]
    assert "kontext für alpha" in texts  # context reclaimed
    assert "zweite" in texts  # queue reclaimed


async def test_late_join_gets_backlog_without_wake(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    for i in range(5):
        await harness.say("frank", "alpha", f"m{i}")
        await harness.state("alpha", "idle")
    beta = await harness.join("beta")
    assert beta.frames("deliver") == []  # no wake on join
    await harness.say("frank", "beta", "jetzt du")
    msgs = harness.delivered("beta")
    assert msgs[-1]["addressing"] == "direct"
    assert sum(1 for m in msgs if m["addressing"] == "overheard") == 5


async def test_done_all_triggers_session_done_and_resets_round(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("alpha", "*", "fertig", kind="done")
    assert harness.clients["frank"].frames("event")[-1]["event"] != "session_done"
    await harness.say("beta", "*", "auch fertig", kind="done")
    events = [f["event"] for f in harness.clients["frank"].frames("event")]
    assert "session_done" in events
    assert harness.hub.round == 0
    # the marks are cleared immediately, not at the next wake
    await harness.hub.handle(harness.clients["frank"], {"t": "roster"})
    roster = harness.clients["frank"].last("roster")
    assert roster is not None
    assert all(q["finished"] is False for q in roster["peers"])
    assert any("session_done" in message for _, message in harness.notifier.calls)


async def test_session_done_fires_again_after_second_round(harness: Harness):
    """No latch: a second round of done says fires session_done again."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("alpha", "*", "fertig", kind="done")
    await harness.say("beta", "*", "auch fertig", kind="done")

    await harness.state("alpha", "idle")
    await harness.state("beta", "idle")
    await harness.say("frank", "alpha", "weiter geht es")  # wakes alpha
    await harness.say("alpha", "*", "wieder fertig", kind="done")
    await harness.say("beta", "*", "ebenfalls wieder fertig", kind="done")
    # beta's closing `done` is queued for the still-busy alpha; the event
    # waits for that queue to drain.
    await harness.state("alpha", "idle")

    done_events = [
        f
        for f in harness.clients["frank"].frames("event")
        if f["event"] == "session_done"
    ]
    assert len(done_events) == 2
    assert harness.hub.round == 0


async def test_session_done_on_disconnect_of_unfinished_agent(harness: Harness):
    """An agent that leaves is no longer counted: the remaining marks decide."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    beta = await harness.join("beta")
    await harness.say("alpha", "*", "fertig", kind="done")
    assert not [
        f
        for f in harness.clients["frank"].frames("event")
        if f["event"] == "session_done"
    ]

    await harness.hub.disconnect(beta)

    done_events = [
        f
        for f in harness.clients["frank"].frames("event")
        if f["event"] == "session_done"
    ]
    assert len(done_events) == 1
    assert harness.hub.round == 0
    assert harness.hub.participants["alpha"].finished is False

    # the last agent leaving alone finds no agents at all: no event
    await harness.hub.disconnect(harness.clients["alpha"])
    done_events = [
        f
        for f in harness.clients["frank"].frames("event")
        if f["event"] == "session_done"
    ]
    assert len(done_events) == 1


# --- W16 session_done ordering


async def _w16_setup(harness: Harness) -> None:
    """frank (observer), alpha and beta both finished, alpha's answer still
    queued for the busy beta. Round 2 at the end: the frank→alpha wake and
    beta's `done` broadcast that woke alpha back up."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.state("beta", "busy")
    await harness.say("frank", "alpha", "arbeite")
    await harness.state("alpha", "idle")
    await harness.say("beta", "*", "fertig", kind="done")
    await harness.say("alpha", "*", "auch fertig", kind="done")


def _session_done(harness: Harness) -> list[dict]:
    return [
        f
        for f in harness.clients["frank"].frames("event")
        if f["event"] == "session_done"
    ]


async def test_session_done_waits_for_the_last_queue_flush(harness: Harness):
    """A `done` still queued for a busy peer defers session_done and the round
    reset until that peer's flush, so the final `done` is delivered under the
    old round budget."""
    await _w16_setup(harness)
    assert harness.hub.participants["beta"].queue  # alpha's done is queued
    assert _session_done(harness) == []
    assert harness.hub.round == 2

    await harness.state("beta", "idle")

    last = harness.clients["beta"].last("deliver")
    assert last is not None
    assert last["round"] == 3
    assert "auch fertig" in [m["text"] for m in last["msgs"]]
    events = _session_done(harness)
    assert len(events) == 1
    assert events[0]["round"] == 3
    assert harness.hub.round == 0


async def test_session_done_not_blocked_by_a_departing_agents_queue(harness: Harness):
    """A queue that leaves with its agent cannot hold the session open."""
    await _w16_setup(harness)
    assert _session_done(harness) == []

    await harness.hub.disconnect(harness.clients["beta"])

    assert len(_session_done(harness)) == 1
    assert harness.hub.round == 0


async def test_late_observer_receives_backlog_as_one_deliver(harness: Harness):
    """A late observer gets the backlog delivered in one frame — its context
    buffer is never drained, so buffering it would swallow it (#26)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    for i in range(3):
        await harness.say("frank", "*", f"b{i}")
    for i in range(2):
        await harness.say("frank", "alpha", f"d{i}")

    bea = await harness.join("bea", kind="observer", caps=[])

    frames = bea.frames("deliver")
    assert len(frames) == 1
    msgs = frames[0]["msgs"]
    assert [m["text"] for m in msgs] == ["b0", "b1", "b2", "d0", "d1"]
    assert [m["id"] for m in msgs] == sorted(m["id"] for m in msgs)
    assert [m["addressing"] for m in msgs] == ["broadcast"] * 3 + ["overheard"] * 2
    assert len(harness.hub.participants["bea"].context) == 0


async def test_reclaiming_observer_receives_backlog(harness: Harness):
    """The restarted operator TUI reclaims its name and still gets the backlog
    (an agent keeps its inherited context instead)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    bea = await harness.join("bea", kind="observer", caps=[])
    for i in range(5):
        await harness.say("frank", "*", f"m{i}")
    assert len(bea.frames("deliver")) == 5

    await harness.hub.disconnect(bea)
    rejoined = await harness.join("bea", kind="observer", caps=[])

    frames = rejoined.frames("deliver")
    assert len(frames) == 1
    assert [m["text"] for m in frames[0]["msgs"]] == [f"m{i}" for i in range(5)]


async def test_dead_registry_pruned_after_ttl(harness: Harness):
    """A dead registration is kept for dead_ttl, then dropped: the next hello
    with that name is a fresh participant (#18)."""
    await harness.join("frank", kind="observer", caps=[])
    alpha = await harness.join("alpha")
    await harness.state("alpha", "busy")
    await harness.say("frank", "alpha", "wartet in der queue")
    assert harness.hub.participants["alpha"].queue

    await harness.hub.disconnect(alpha)
    assert harness.hub.dead["alpha"].queue

    harness.clock.advance(harness.config.dead_ttl + 1)
    await harness.hub.watchdog_tick()
    assert "alpha" not in harness.hub.dead

    client = await harness.join("alpha")
    assert harness.hub.participants["alpha"].queue == []
    assert client.frames("deliver") == []


async def test_mute_blocks_agent_to_agent_only(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.cmd("frank", "mute")
    await harness.say("alpha", "beta", "gesperrt")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_MUTED
    # agent → observer and observer → agent stay open while muted
    await harness.say("alpha", "frank", "an observer geht")
    reply = harness.ok_or_err("alpha")
    assert reply is not None and reply["t"] == "ok"
    assert "an observer geht" in [m["text"] for m in harness.delivered("frank")]
    await harness.state("alpha", "idle")  # the mute system message woke alpha
    await harness.say("frank", "alpha", "von observer geht")
    assert "von observer geht" in [m["text"] for m in harness.delivered("alpha")]
    await harness.cmd("frank", "unmute")
    harness.clients["alpha"].clear()
    await harness.say("alpha", "beta", "wieder frei")
    assert harness.clients["alpha"].last("err") is None
    # agents learned about unmute via system message
    sys_msgs = [
        m
        for m in harness.delivered("alpha") + harness.delivered("beta")
        if m["from"] == "system"
    ]
    assert any("mute" in m["text"] for m in sys_msgs)


async def test_four_participants(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    for name in ("alpha", "beta", "gamma"):
        await harness.join(name)
    await harness.say("frank", "*", "alle")
    for name in ("alpha", "beta", "gamma"):
        assert len(harness.delivered(name)) == 1
    assert harness.hub.round == 1  # three wakes, one occasion


async def test_stall_fires_exactly_once(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.clock.advance(120.0)
    await harness.hub.watchdog_tick()
    await harness.hub.watchdog_tick()
    await harness.hub.watchdog_tick()
    stalls = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    assert len(stalls) == 1


async def test_blocked_notification(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.state("alpha", "blocked", detail="permission: bash")
    harness.clock.advance(61.0)
    await harness.hub.watchdog_tick()
    blocked = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "blocked"
    ]
    assert len(blocked) == 1 and blocked[0]["detail"] == "permission: bash"


async def test_state_inference_without_idle_events(harness: Harness):
    """Capability-less participant: busy after deliver, idle on say, idle on
    timeout with stall hint."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("cc", kind="claude-code", caps=[])
    await harness.say("frank", "cc", "wake")
    assert harness.hub.participants["cc"].state == "busy"
    await harness.say("cc", "frank", "antwort")
    assert harness.hub.participants["cc"].state == "idle"
    await harness.say("frank", "cc", "nochmal")  # busy again
    harness.clock.advance(91.0)
    await harness.hub.watchdog_tick()
    assert harness.hub.participants["cc"].state == "idle"


async def test_delivery_to_long_silent_agent_is_not_flipped_idle(harness: Harness):
    """Observed in a live run: an agent that had been silent for minutes was
    assumed idle on the first tick after a delivery, while it was working on
    it. The assume-idle timer counts from the delivery, not from the last
    inbound frame."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("cc", kind="claude-code", caps=[])
    await harness.say("cc", "frank", "last frame from cc")  # cc idle, last_rx = now
    harness.clock.advance(200.0)  # long silence (< dead_after), still idle
    await harness.say("frank", "cc", "new task")  # deliver -> busy
    harness.clock.advance(5.0)
    await harness.hub.watchdog_tick()
    assert harness.hub.participants["cc"].state == "busy"
    assert not [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    harness.clock.advance(90.0)  # 95 s after the delivery: now the rule applies
    await harness.hub.watchdog_tick()
    assert harness.hub.participants["cc"].state == "idle"
    hints = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    assert any("assumed idle" in f["detail"] for f in hints)


async def test_pong_does_not_restart_assume_idle_timer(harness: Harness):
    """Observed in a live run: the hub pinged a long-quiet agent mid-turn, the
    spoke answered, and the pong pushed the assume-idle flip out by up to
    300 s. Liveness frames are not output — only the delivery time counts."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("cc", kind="claude-code", caps=[])
    await harness.say("cc", "frank", "hi")  # last frame from cc at t=0
    harness.clock.advance(280.0)
    await harness.say("frank", "cc", "task")  # delivery at t=280 -> busy
    harness.clock.advance(25.0)  # t=305: >300 s since cc's last frame -> ping
    await harness.hub.watchdog_tick()
    assert harness.clients["cc"].last("ping") is not None
    await harness.hub.handle(harness.clients["cc"], {"t": "pong"})  # t=305
    harness.clock.advance(70.0)  # t=375: 95 s after the delivery
    await harness.hub.watchdog_tick()
    assert harness.hub.participants["cc"].state == "idle"  # not pushed to 395+


async def test_state_busy_restarts_assume_idle_timer(harness: Harness):
    """A capability-less spoke that reports `state busy` is trusted: the 90 s
    window runs from that report."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("cc", kind="claude-code", caps=[])
    await harness.say("frank", "cc", "task")  # delivery at t=0 -> busy
    harness.clock.advance(80.0)
    await harness.state("cc", "busy")  # t=80: still working
    harness.clock.advance(15.0)  # t=95: 95 s after delivery, 15 s after report
    await harness.hub.watchdog_tick()
    assert harness.hub.participants["cc"].state == "busy"
    harness.clock.advance(80.0)  # t=175: 95 s after the report
    await harness.hub.watchdog_tick()
    assert harness.hub.participants["cc"].state == "idle"


async def test_observer_deliver_carries_the_wake_round(harness: Harness):
    """The observer's copy of a wake-causing message shows the same round as
    the woken agent's copy (it used to lag by one)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("alpha", "beta", "wakes beta: round 1")
    frank_frame = harness.clients["frank"].frames("deliver")[-1]
    beta_frame = harness.clients["beta"].frames("deliver")[-1]
    assert beta_frame["round"] == 1
    assert frank_frame["round"] == beta_frame["round"]


async def test_ping_pong_dead_detection(harness: Harness):
    """Silent connection > dead_after gets pinged; no pong within the timeout
    disconnects and keeps state reclaimable (see docs/PROTOCOL.md "ping")."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.clock.advance(301.0)
    await harness.hub.watchdog_tick()
    assert harness.clients["alpha"].last("ping") is not None
    harness.clock.advance(31.0)
    await harness.hub.watchdog_tick()
    assert "alpha" not in harness.hub.participants
    assert "alpha" in harness.hub.dead  # reclaimable
    # a pong in time would have kept the connection: repeat with response
    client = await harness.join("alpha")  # reclaim
    harness.clock.advance(301.0)
    await harness.hub.watchdog_tick()
    await harness.hub.handle(client, {"t": "pong"})
    harness.clock.advance(31.0)
    await harness.hub.watchdog_tick()
    assert "alpha" in harness.hub.participants  # survived


async def test_room_recorded_not_routed(harness: Harness):
    """v1: room is validated and logged, not routed (isolation test is v1.1)."""
    await harness.join("frank", kind="observer", caps=[], room="mover")
    await harness.join("alpha", room="mover")
    await harness.join("beta", room="other")
    await harness.say("frank", "*", "übergreifend")
    assert harness.delivered("beta")  # no isolation in v1
    records = harness.transcript.read_today()
    assert any(
        r.get("event") == "room_declared" and r.get("room") == "other" for r in records
    )


async def test_restart_rebuilds_context_from_transcript(tmp_path):
    """After a hub restart, a reclaiming participant's context is rebuilt from
    the transcript (see docs/ARCHITECTURE.md "Registry, reconnect, restart")."""
    from moot.core.clock import FakeClock
    from moot.core.config import Config
    from moot.core.hub import Hub
    from moot.core.notify import CollectingNotifier
    from moot.core.transcript import Transcript

    config = Config(home=tmp_path)
    clock = FakeClock()
    h1 = Harness(tmp_path, config)
    h1.clock = clock
    h1.hub = Hub(
        config, clock, Transcript(config.transcript_dir, clock), CollectingNotifier()
    )
    await h1.join("frank", kind="observer", caps=[])
    await h1.join("alpha")
    await h1.say("frank", "alpha", "wichtiger kontext")

    # restart: brand-new hub over the same home dir
    h2 = Harness(tmp_path, config)
    h2.clock = clock
    h2.hub = Hub(
        config, clock, Transcript(config.transcript_dir, clock), CollectingNotifier()
    )
    await h2.join("frank", kind="observer", caps=[])
    client = await h2.join("alpha")
    await h2.say("frank", "alpha", "wake nach restart")
    msgs = [m for f in client.frames("deliver") for m in f["msgs"]]
    texts = [m["text"] for m in msgs]
    assert "wichtiger kontext" in texts and "wake nach restart" in texts


# --------------------------------------------------------------- Phase 5


async def test_muted_rejection_does_not_poison_dedup(harness: Harness):
    """Mute is checked before the dedup window, so a rejected say may be
    repeated verbatim once mute is lifted (#12)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.cmd("frank", "mute")

    await harness.say("alpha", "beta", "hallo")
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_MUTED

    await harness.cmd("frank", "unmute")
    harness.clients["alpha"].clear()
    await harness.say("alpha", "beta", "hallo")
    assert harness.clients["alpha"].last("err") is None
    assert harness.clients["alpha"].last("ok") is not None


async def test_unknown_recipient_rejection_costs_no_token(harness: Harness):
    """An unroutable say is rejected before the token bucket sees it (#12)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    for i in range(3):
        await harness.say("alpha", "gamma", f"phantom {i}")
    errs = harness.clients["alpha"].frames("err")
    assert [e["code"] for e in errs] == [proto.ERR_UNKNOWN_RECIPIENT] * 3
    bucket = harness.hub.participants["alpha"].bucket
    assert bucket.retry_after() == 0.0

    harness.clients["alpha"].clear()
    for i in range(3):  # the full burst is still available
        await harness.say("alpha", "frank", f"m{i}")
    assert harness.clients["alpha"].frames("err") == []
    assert len(harness.clients["alpha"].frames("ok")) == 3


async def test_rejected_say_still_infers_idle(harness: Harness):
    """A say *frame* proves activity even when the hub rejects it (#21)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("cc", kind="claude-code", caps=[])
    await harness.say("frank", "cc", "wake")
    assert harness.hub.participants["cc"].state == "busy"

    await harness.say("cc", "gamma", "an ein phantom")
    err = harness.clients["cc"].last("err")
    assert err is not None and err["code"] == proto.ERR_UNKNOWN_RECIPIENT
    assert harness.hub.participants["cc"].state == "idle"

    # …and the inferred idle transition flushes the pending queue. (After the
    # flush cc is busy again, so the two effects are asserted separately.)
    await harness.say("frank", "cc", "nochmal wach")
    await harness.say("frank", "cc", "wartet in der queue")
    assert harness.hub.participants["cc"].queue
    harness.clients["cc"].clear()
    await harness.say("cc", "gamma", "noch ein phantom")
    assert [m["text"] for m in harness.delivered("cc")] == ["wartet in der queue"]


async def test_blocked_heartbeat_still_notifies(harness: Harness):
    """A spoke that repeats `blocked` as a heartbeat must not reset the
    watchdog timer (#19) — one event per blocked episode."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.state("alpha", "blocked", detail="permission: bash")
    for _ in range(6):  # 3 minutes of 30 s heartbeats
        harness.clock.advance(30.0)
        await harness.hub.watchdog_tick()
        await harness.state("alpha", "blocked", detail="permission: bash")

    blocked = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "blocked"
    ]
    assert len(blocked) == 1 and blocked[0]["detail"] == "permission: bash"


async def test_blocked_detail_change_rearms(harness: Harness):
    """A new blocked reason is a new episode: the timer restarts and a second
    notification is allowed (#19)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.state("alpha", "blocked", detail="permission: bash")
    harness.clock.advance(61.0)
    await harness.hub.watchdog_tick()

    await harness.state("alpha", "blocked", detail="waiting for review")
    harness.clock.advance(61.0)
    await harness.hub.watchdog_tick()

    blocked = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "blocked"
    ]
    assert [f["detail"] for f in blocked] == ["permission: bash", "waiting for review"]


async def test_capless_blocked_recovers_on_timeout_and_on_say(harness: Harness):
    """A capability-less spoke that reported `blocked` leaves that state by the
    same two rules as busy: 90 s silence, or its next say (#20, #58)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("cc", kind="claude-code", caps=[])
    await harness.state("cc", "blocked", detail="permission: bash")

    harness.clock.advance(91.0)
    await harness.hub.watchdog_tick()
    p = harness.hub.participants["cc"]
    assert p.state == "idle"
    assert p.blocked_since is None and p.blocked_detail is None

    await harness.state("cc", "blocked", detail="permission: bash")
    assert p.state == "blocked"
    await harness.say("cc", "frank", "wieder da")
    assert p.state == "idle"
    assert p.blocked_since is None and p.blocked_detail is None


async def test_late_join_gets_exactly_backlog_on_join(harness: Harness):
    """The late-join backlog is backlog_on_join messages, not the whole recent
    window — so no eviction placeholder rides along (#15)."""
    harness.hub.max_rounds = 1000
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    for i in range(30):
        await harness.say("frank", "alpha", f"m{i}")
        await harness.state("alpha", "idle")

    await harness.join("beta")
    cap = harness.config.backlog_on_join
    assert len(harness.hub.participants["beta"].context) == cap

    await harness.say("frank", "beta", "jetzt du")
    msgs = harness.delivered("beta")
    assert [m["addressing"] for m in msgs] == ["overheard"] * cap + ["direct"]
    assert not any(m["from"] == proto.SYSTEM_SENDER for m in msgs)
    assert [m["text"] for m in msgs[:cap]] == [f"m{i}" for i in range(30 - cap, 30)]


async def test_restart_continues_message_ids(tmp_path):
    """The id counter is re-seeded from the transcript, so ids stay unique
    across hub restarts (#27)."""
    from moot.core.clock import FakeClock
    from moot.core.config import Config
    from moot.core.hub import Hub
    from moot.core.notify import CollectingNotifier
    from moot.core.transcript import Transcript

    config = Config(home=tmp_path)
    clock = FakeClock()

    def restart() -> Harness:
        h = Harness(tmp_path, config)
        h.clock = clock
        h.transcript = Transcript(config.transcript_dir, clock)
        h.hub = Hub(config, clock, h.transcript, CollectingNotifier())
        return h

    h1 = restart()
    await h1.join("frank", kind="observer", caps=[])
    await h1.join("alpha")
    await h1.say("frank", "alpha", "eins")
    await h1.state("alpha", "idle")
    await h1.say("frank", "alpha", "zwei")
    last_id = max(m["id"] for m in h1.delivered("alpha"))
    assert last_id == 2

    h2 = restart()
    await h2.join("frank", kind="observer", caps=[])
    client = await h2.join("alpha")
    await h2.say("frank", "alpha", "drei")

    msgs = [m for f in client.frames("deliver") for m in f["msgs"]]
    assert [m["text"] for m in msgs] == ["eins", "zwei", "drei"]
    ids = [m["id"] for m in msgs]
    assert ids == sorted(ids)  # backlog + new message strictly increasing
    assert ids[-1] == last_id + 1


async def test_batch_order_survives_backwards_wall_clock(harness: Harness, monkeypatch):
    """A coalesced batch is ordered by message id, not by wall clock (#33)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.state("alpha", "busy")
    await harness.say("frank", "alpha", "erste")
    monkeypatch.setattr(harness.clock, "wall", lambda: 1.0)  # clock jumps back
    await harness.say("frank", "alpha", "zweite")
    monkeypatch.undo()

    await harness.state("alpha", "idle")
    msgs = harness.delivered("alpha")
    assert [m["text"] for m in msgs] == ["erste", "zweite"]
    assert [m["id"] for m in msgs] == sorted(m["id"] for m in msgs)
    assert msgs[0]["ts"] > msgs[1]["ts"]  # the wall clock really did go back


async def test_wake_cap_keeps_cross_frame_fifo(harness: Harness):
    """Context newer than the wake slice must not overtake still-queued wakes
    (#13): the context drain is bounded by the last id of the slice."""
    harness.hub.max_rounds = 1000
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.state("alpha", "busy")
    await harness.state("beta", "busy")
    for i in range(1, 13):
        await harness.say("frank", "alpha", f"d{i}")
    await harness.say("frank", "beta", "o")  # overheard for alpha, newest id

    await harness.state("alpha", "idle")
    frame1 = harness.clients["alpha"].frames("deliver")[-1]
    assert [m["text"] for m in frame1["msgs"]] == [f"d{i}" for i in range(1, 11)]

    await harness.state("alpha", "idle")
    frame2 = harness.clients["alpha"].frames("deliver")[-1]
    assert [m["text"] for m in frame2["msgs"]] == ["d11", "d12", "o"]


async def test_wake_cap_keeps_cross_frame_fifo_with_context_in_the_middle(
    harness: Harness,
):
    """Same as above with the overheard message between d10 and d11: it rides
    in the second frame, ahead of the wakes it preceded (#13)."""
    harness.hub.max_rounds = 1000
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.state("alpha", "busy")
    await harness.state("beta", "busy")
    for i in range(1, 11):
        await harness.say("frank", "alpha", f"d{i}")
    await harness.say("frank", "beta", "o")
    for i in range(11, 13):
        await harness.say("frank", "alpha", f"d{i}")

    await harness.state("alpha", "idle")
    frame1 = harness.clients["alpha"].frames("deliver")[-1]
    assert [m["text"] for m in frame1["msgs"]] == [f"d{i}" for i in range(1, 11)]

    await harness.state("alpha", "idle")
    frame2 = harness.clients["alpha"].frames("deliver")[-1]
    assert [m["text"] for m in frame2["msgs"]] == ["o", "d11", "d12"]


async def test_system_only_flush_is_not_a_round(harness: Harness):
    """A wake slice with nothing but system messages costs no round (#28)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.state("alpha", "busy")

    await harness.cmd("frank", "mute")  # system message queued for alpha
    assert harness.hub.round == 0
    await harness.state("alpha", "idle")
    assert harness.hub.round == 0
    assert [m["from"] for m in harness.delivered("alpha")] == [proto.SYSTEM_SENDER]

    # a mixed slice charges exactly one round
    await harness.say("frank", "alpha", "echte nachricht")  # queued: alpha busy
    await harness.cmd("frank", "unmute")  # system message queued too
    assert harness.hub.round == 0
    await harness.state("alpha", "idle")
    assert harness.hub.round == 1


async def test_roster_polling_does_not_suppress_stall(harness: Harness):
    """An observer polling the roster is not conversation (#22)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    for _ in range(36):  # 3 minutes
        harness.clock.advance(5.0)
        await harness.hub.handle(harness.clients["frank"], {"t": "roster"})
        await harness.hub.watchdog_tick()

    stalls = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    assert len(stalls) == 1


async def test_pong_does_not_suppress_stall(harness: Harness):
    """Nor is a keepalive pong (#22)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    for _ in range(36):
        harness.clock.advance(5.0)
        await harness.hub.handle(harness.clients["alpha"], {"t": "pong"})
        await harness.hub.watchdog_tick()

    stalls = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    assert len(stalls) == 1


async def test_queue_cap_drops_oldest_with_placeholder(tmp_path):
    """The wake queue is bounded: the oldest entries are dropped and the loss
    is reported to the recipient (#18)."""
    from moot.core.config import Config

    config = Config(home=tmp_path)
    config.queue_cap = 3
    harness = Harness(tmp_path, config)
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.state("alpha", "busy")
    for i in range(5):
        await harness.say("frank", "alpha", f"m{i}")

    p = harness.hub.participants["alpha"]
    assert [m.text for m in p.queue] == ["m2", "m3", "m4"]
    assert p.queue_dropped == 2

    await harness.state("alpha", "idle")
    msgs = harness.delivered("alpha")
    assert msgs[0]["from"] == proto.SYSTEM_SENDER
    assert msgs[0]["addressing"] == "direct"
    assert "2 addressed messages dropped" in msgs[0]["text"]
    assert [m["text"] for m in msgs[1:]] == ["m2", "m3", "m4"]
    assert p.queue_dropped == 0


async def test_dead_recipient_does_not_affect_sender(harness: Harness):
    """A recipient whose transport is already gone is queued, not delivered to
    — the sender and the other recipients are unaffected (#2)."""
    from tests.conftest import DyingClient

    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    beta = await harness.join_with(DyingClient("welcome"), "beta")
    await harness.join("gamma")
    assert beta.connected is False

    await harness.say("alpha", "*", "an alle")
    ok = harness.clients["alpha"].last("ok")
    assert ok is not None and ok["queued"] == 1
    assert [m["text"] for m in harness.delivered("gamma")] == ["an alle"]
    assert [m.text for m in harness.hub.participants["beta"].queue] == ["an alle"]
    assert harness.hub.round == 1

    await harness.hub.disconnect(beta)
    fresh = await harness.join("beta")
    assert [m["text"] for m in harness.delivered("beta")] == ["an alle"]
    assert fresh.last("err") is None


async def test_recipient_dying_mid_delivery(harness: Harness):
    """A transport that dies while the deliver frame is written loses that
    frame (as on a real socket) but nothing else (#2)."""
    from tests.conftest import DyingClient

    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    beta = await harness.join_with(DyingClient("deliver"), "beta")
    await harness.join("gamma")

    await harness.say("alpha", "*", "an alle")
    assert harness.clients["alpha"].last("ok") is not None
    assert [m["text"] for m in harness.delivered("gamma")] == ["an alle"]
    assert [f["t"] for f in beta.dropped] == ["deliver"]
    assert beta.connected is False

    await harness.say("alpha", "beta", "direkt")
    assert [m.text for m in harness.hub.participants["beta"].queue] == ["direkt"]

    await harness.hub.watchdog_tick()
    assert "beta" not in harness.hub.participants
    assert "beta" in harness.hub.dead


async def test_dying_observer_does_not_affect_agents(harness: Harness):
    """An observer whose transport is gone is skipped, not delivered to (#2)."""
    from tests.conftest import DyingClient

    frank = await harness.join_with(
        DyingClient("welcome"), "frank", kind="observer", caps=[]
    )
    await harness.join("alpha")
    await harness.join("beta")
    assert frank.connected is False

    await harness.say("alpha", "*", "an alle")
    assert harness.clients["alpha"].last("ok") is not None
    assert [m["text"] for m in harness.delivered("beta")] == ["an alle"]
    assert harness.delivered("frank") == []
    # the hub skipped the dead observer instead of writing into the void
    assert [f["t"] for f in frank.dropped] == ["welcome"]


async def test_freeze_without_observer_logs_warning(harness: Harness, caplog):
    """A freeze nobody can lift is a stderr WARNING, not only a transcript
    event (#30)."""
    await harness.join("alpha")
    await harness.join("beta")
    harness.hub.max_rounds = 1
    with caplog.at_level(logging.WARNING, logger="moot.hub"):
        await harness.say("alpha", "beta", "runde 1")
    assert harness.hub.frozen is True
    assert "no observer" in caplog.text
    records = [
        json.loads(line)
        for path in sorted(harness.config.transcript_dir.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(r.get("event") == "freeze_without_observer" for r in records)


async def test_interleaved_handles_do_not_corrupt_roster(harness: Harness):
    """Two hub.handle() calls interleaved at an await inside a fan-out loop:
    snapshot iteration keeps the roster intact. The server layer additionally
    serialises every handle() with one lock (P6.3)."""
    await harness.join_with(YieldingClient(), "frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")

    gamma_client = FakeClient()
    await asyncio.gather(
        harness.hub.handle(
            harness.clients["alpha"],
            {"t": "say", "to": "*", "kind": "note", "text": "an alle", "seq": 99},
        ),
        harness.hub.handle(
            gamma_client,
            {
                "t": "hello",
                "proto": 1,
                "name": "gamma",
                "kind": "opencode",
                "caps": ["idle-events"],
            },
        ),
    )

    assert set(harness.hub.participants) == {"frank", "alpha", "beta", "gamma"}
    assert gamma_client.last("welcome") is not None
    assert harness.clients["alpha"].last("ok") is not None
    assert [m["text"] for m in harness.delivered("beta")] == ["an alle"]


# --------------------------------------------------------------- Phase 7


async def test_restart_after_midnight_keeps_yesterdays_backlog(tmp_path):
    """The reseed window covers yesterday and today, so a hub restarted just
    after midnight keeps its backlog and its id counter (#57)."""
    from moot.core.clock import FakeClock
    from moot.core.config import Config
    from moot.core.hub import Hub
    from moot.core.notify import CollectingNotifier
    from moot.core.transcript import Transcript

    config = Config(home=tmp_path)
    clock = FakeClock()

    def restart() -> Harness:
        h = Harness(tmp_path, config)
        h.clock = clock
        h.transcript = Transcript(config.transcript_dir, clock)
        h.hub = Hub(config, clock, h.transcript, CollectingNotifier())
        return h

    h1 = restart()
    await h1.join("frank", kind="observer", caps=[])
    await h1.join("alpha")
    for i in range(3):
        await h1.say("frank", "alpha", f"gestern-{i}")
        await h1.state("alpha", "idle")
    clock.advance(86_400)  # midnight passes
    await h1.say("frank", "alpha", "heute")

    h2 = restart()
    assert [r["text"] for r in h2.hub._recent] == [
        "gestern-0",
        "gestern-1",
        "gestern-2",
        "heute",
    ]
    assert [r["id"] for r in h2.hub._recent] == [1, 2, 3, 4]
    assert h2.hub._msg_counter == 4  # ids continue instead of restarting at 1


# --------------------------------------------------------------- Phase 8


async def test_context_eviction_end_to_end(tmp_path):
    """An overflowing context buffer announces the loss on the next wake: the
    placeholder leads the batch and is recorded in the transcript (#66)."""
    from moot.core.config import Config

    config = Config(home=tmp_path)
    config.context_cap = 3
    harness = Harness(tmp_path, config)
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    for i in range(5):
        await harness.say("frank", "beta", f"overheard-{i}")
        await harness.state("beta", "idle")

    await harness.say("frank", "alpha", "aufwachen")
    msgs = harness.delivered("alpha")
    assert msgs[0]["from"] == proto.SYSTEM_SENDER
    assert msgs[0]["id"] == 0
    assert msgs[0]["addressing"] == "overheard"
    assert msgs[0]["text"].startswith("2 older messages")
    assert [m["text"] for m in msgs[1:]] == [
        "overheard-2",
        "overheard-3",
        "overheard-4",
        "aufwachen",
    ]
    delivers = [
        r
        for r in harness.transcript.read_today()
        if r.get("type") == "deliver" and r.get("to") == "alpha"
    ]
    assert delivers[-1]["msg_ids"] == [0, 3, 4, 5, 6]
    # the placeholder and the three context lines; #6 is the wake
    assert delivers[-1]["overheard"] == [0, 3, 4, 5]


async def test_observer_receives_overheard_only_frames(harness: Harness):
    """Observers see everything immediately, including a deliver whose only
    message is overheard — the wake invariant does not apply to them (#67)."""
    frank = await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    frank.clear()

    await harness.say("alpha", "beta", "unter vier augen")
    frames = frank.frames("deliver")
    assert len(frames) == 1
    assert [m["addressing"] for m in frames[0]["msgs"]] == ["overheard"]
    assert [m["text"] for m in frames[0]["msgs"]] == ["unter vier augen"]


async def test_welcome_payload(harness: Harness):
    """welcome carries the roster and the limits a spoke needs (#68)."""
    await harness.join("alpha", role="builder")
    frank = await harness.join("frank", kind="observer", caps=[])
    welcome = frank.last("welcome")
    assert welcome is not None
    assert welcome["name"] == "frank"
    assert welcome["round"] == 0
    assert welcome["peers"] == [
        {"name": "alpha", "kind": "opencode", "role": "builder", "state": "idle"}
    ]
    assert welcome["limits"] == {"rate": "6/60s", "max_rounds": 24}


REJECT_SEQ = 4242


async def _say_with_seq(h: Harness, sender: str, to: str, text: str) -> None:
    await h.hub.handle(
        h.clients[sender],
        {"t": "say", "to": to, "kind": "note", "text": text, "seq": REJECT_SEQ},
    )


async def _reject_frozen(h: Harness) -> None:
    await h.cmd("frank", "freeze")
    await _say_with_seq(h, "alpha", "beta", "nach dem freeze")


async def _reject_rate_limited(h: Harness) -> None:
    for i in range(h.config.rate_burst):
        await h.say("alpha", "beta", f"burst-{i}")
    await _say_with_seq(h, "alpha", "beta", "eins zu viel")


async def _reject_duplicate(h: Harness) -> None:
    await h.say("alpha", "beta", "zweimal dasselbe")
    await _say_with_seq(h, "alpha", "beta", "zweimal dasselbe")


async def _reject_muted(h: Harness) -> None:
    await h.cmd("frank", "mute")
    await _say_with_seq(h, "alpha", "beta", "gesperrt")


async def _reject_unknown_recipient(h: Harness) -> None:
    await _say_with_seq(h, "alpha", "niemand", "wohin damit?")


async def _reject_forbidden(h: Harness) -> None:
    await h.hub.handle(
        h.clients["alpha"], {"t": "cmd", "cmd": "freeze", "seq": REJECT_SEQ}
    )


@pytest.mark.parametrize(
    ("code", "scenario"),
    [
        (proto.ERR_FROZEN, _reject_frozen),
        (proto.ERR_RATE_LIMITED, _reject_rate_limited),
        (proto.ERR_DUPLICATE, _reject_duplicate),
        (proto.ERR_MUTED, _reject_muted),
        (proto.ERR_UNKNOWN_RECIPIENT, _reject_unknown_recipient),
        (proto.ERR_FORBIDDEN, _reject_forbidden),
    ],
)
async def test_err_seq_echo_on_every_rejection(harness: Harness, code, scenario):
    """Every rejection echoes the sender's seq, so a spoke can correlate the
    err with the frame it sent (#65)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await scenario(harness)

    err = harness.clients["alpha"].last("err")
    assert err is not None
    assert err["code"] == code
    assert err["seq"] == REJECT_SEQ


async def test_deliver_wake_cap_remainder_is_delivered_next_idle(harness: Harness):
    """A deliver frame carries at most deliver_wake_cap wake messages; the
    remainder rides on the next idle transition, in order."""
    await harness.join("frank", kind="observer", caps=[])
    beta = await harness.join("beta")
    await harness.state("beta", "busy")
    for i in range(12):
        await harness.say("frank", "beta", f"m{i}")
    assert len(harness.hub.participants["beta"].queue) == 12

    await harness.state("beta", "idle")
    await harness.state("beta", "idle")  # the first deliver put beta back to busy
    frames = beta.frames("deliver")
    assert [len(f["msgs"]) for f in frames] == [10, 2]
    texts = [m["text"] for m in harness.delivered("beta")]
    assert texts == [f"m{i}" for i in range(12)]
    assert harness.hub.participants["beta"].queue == []


async def test_hub_constructed_directly_writes_no_session_record(harness: Harness):
    """`session`/`session_end` are written by serve(), not by the hub:
    an embedded Hub leaves the transcript free of run bookkeeping."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("frank", "alpha", "hallo")

    files = sorted(harness.config.transcript_dir.glob("*.jsonl"))
    records = [
        json.loads(line) for f in files for line in f.read_text().splitlines() if line
    ]
    assert records  # the say and its deliver are there
    assert not [r for r in records if r["type"] in ("session", "session_end")]
    assert not [r for r in records if r.get("event") == "config"]


# ------------------------------------------------------- W8 rejected event


@pytest.mark.parametrize(
    ("code", "scenario", "to", "text"),
    [
        (proto.ERR_FROZEN, _reject_frozen, "beta", "nach dem freeze"),
        (proto.ERR_MUTED, _reject_muted, "beta", "gesperrt"),
        (
            proto.ERR_UNKNOWN_RECIPIENT,
            _reject_unknown_recipient,
            "niemand",
            "wohin damit?",
        ),
        (proto.ERR_RATE_LIMITED, _reject_rate_limited, "beta", "eins zu viel"),
        (proto.ERR_DUPLICATE, _reject_duplicate, "beta", "zweimal dasselbe"),
    ],
)
async def test_every_rejection_emits_a_rejected_event(
    harness: Harness, code, scenario, to, text
):
    """A refused say is invisible to everyone but its sender unless the hub
    says so: observers get one `rejected` event carrying who, why, and how
    big — never the text itself."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await scenario(harness)

    events = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "rejected"
    ]
    assert len(events) == 1
    event = events[0]
    assert event["name"] == "alpha"
    assert event["code"] == code
    assert event["to"] == to
    assert event["kind"] == "note"
    assert event["bytes"] == len(text.encode("utf-8"))
    assert "text" not in event


async def test_rejected_event_carries_retry_after_on_rate_limit(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await _reject_rate_limited(harness)
    await _reject_frozen(harness)  # checked before the bucket: a second code

    events = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "rejected"
    ]
    rate = [e for e in events if e["code"] == proto.ERR_RATE_LIMITED]
    assert len(rate) == 1 and rate[0]["retry_after"] == pytest.approx(10.0)
    frozen = [e for e in events if e["code"] == proto.ERR_FROZEN]
    assert len(frozen) == 1 and "retry_after" not in frozen[0]


async def test_observer_rejection_emits_no_event(harness: Harness):
    """An observer's own refused say stays between the hub and the seat."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await _say_with_seq(harness, "frank", "niemand", "wohin damit?")

    err = harness.clients["frank"].last("err")
    assert err is not None and err["code"] == proto.ERR_UNKNOWN_RECIPIENT
    assert not [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "rejected"
    ]


async def test_rejected_event_is_transcribed(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await _reject_muted(harness)

    records = [
        r for r in harness.transcript.read_today() if r.get("event") == "rejected"
    ]
    assert len(records) == 1
    assert records[0]["name"] == "alpha"
    assert records[0]["code"] == proto.ERR_MUTED
    assert "text" not in records[0]


# --------------------------------------------------- W5a notify the human


async def test_direct_say_to_an_observer_notifies(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.clock.advance(61)  # frank's hello is 61 s old: nobody at the seat
    await harness.say("alpha", "frank", "wo hakt es?", kind="question")

    assert ("moot", "alpha → frank · question: wo hakt es?") in harness.notifier.calls


async def test_broadcast_question_notifies(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    harness.clock.advance(61)  # frank's hello is 61 s old: nobody at the seat
    await harness.say("alpha", "*", "wer hat die zahlen?", kind="question")
    await harness.say("alpha", "*", "die migration lief zuletzt", kind="claim")

    assert (
        "moot",
        "alpha → * · question: wer hat die zahlen?",
    ) in harness.notifier.calls
    assert not [
        c for c in harness.notifier.calls if c[1].startswith("alpha → * · claim")
    ]


async def test_agent_to_agent_direct_does_not_notify(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("alpha", "beta", "wo hakt es?", kind="question")

    assert not [c for c in harness.notifier.calls if c[1].startswith("alpha →")]


async def test_observer_say_does_not_notify(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("frank", "alpha", "wo hakt es?", kind="question")

    assert not [c for c in harness.notifier.calls if c[1].startswith("frank →")]


async def test_notification_text_is_clipped_and_single_line(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.clock.advance(61)  # frank's hello is 61 s old: nobody at the seat
    await harness.say("alpha", "frank", "erste zeile\nzweite zeile " + "a" * 100)

    head = f"erste zeile zweite zeile {'a' * 55}"
    assert len(head) == 80
    assert ("moot", f"alpha → frank · note: {head}") in harness.notifier.calls


async def test_no_notify_config_suppresses_it(harness: Harness):
    harness.config.notifications = False
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.clock.advance(61)
    await harness.say("alpha", "frank", "wo hakt es?", kind="question")

    assert harness.notifier.calls == []


def _says_from(harness: Harness, sender: str) -> list[str]:
    return [c[1] for c in harness.notifier.calls if c[1].startswith(f"{sender} →")]


async def test_direct_say_to_a_present_observer_does_not_notify(harness: Harness):
    """The TUI polls the roster every 3 s; a seat that sent a frame in the
    last 60 s has a human behind it — no popup."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.clock.advance(61)
    await harness.roster("frank")
    await harness.say("alpha", "frank", "wo hakt es?", kind="question")
    assert _says_from(harness, "alpha") == []

    harness.clock.advance(61)
    await harness.say("alpha", "frank", "noch da?", kind="question")
    assert _says_from(harness, "alpha") == ["alpha → frank · question: noch da?"]


async def test_presence_is_per_seat_for_direct_says(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("gerd", kind="observer", caps=[])
    await harness.join("alpha")
    harness.clock.advance(61)
    await harness.roster("gerd")
    await harness.say("alpha", "frank", "frank?", kind="question")
    await harness.say("alpha", "gerd", "gerd?", kind="question")

    assert _says_from(harness, "alpha") == ["alpha → frank · question: frank?"]


async def test_broadcast_question_is_silent_while_any_observer_is_present(
    harness: Harness,
):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("gerd", kind="observer", caps=[])
    await harness.join("alpha")
    harness.clock.advance(61)
    await harness.roster("gerd")
    await harness.say("alpha", "*", "wer hat die zahlen?", kind="question")
    assert _says_from(harness, "alpha") == []

    harness.clock.advance(61)
    await harness.say("alpha", "*", "niemand?", kind="question")
    assert _says_from(harness, "alpha") == ["alpha → * · question: niemand?"]


async def test_quorum_notification_follows_the_presence_rule(harness: Harness):
    await _parley(harness)
    await _answer(harness, "alpha", "PLEDGE r1: build")
    await _answer(harness, "beta", "PLEDGE r1: guard")
    await harness.roster("frank")  # the seat's poll: somebody is reading
    await harness.hub.watchdog_tick()

    assert len(_quorums(harness)) == 1
    assert not [c for c in harness.notifier.calls if c[1].startswith("quorum")]


# ----------------------------------------------------------- W11 stall detail


async def test_stall_detail_names_done_and_not_done(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("beta", "*", "fertig", kind="done")
    await harness.state("alpha", "idle")
    harness.clock.advance(120)
    await harness.hub.watchdog_tick()

    stalls = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    assert len(stalls) == 1
    assert stalls[0]["detail"] == (
        "all agents idle, nothing queued — unread context: —; "
        "done: beta; not done: alpha"
    )


async def test_stall_detail_reports_unread_context(harness: Harness):
    """frank's direct say to alpha is overheard context for beta; nobody woke
    beta for it, and the stall names that instead of claiming nothing is
    pending."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "alpha", "guck mal")
    await harness.state("alpha", "idle")
    harness.clock.advance(120)
    await harness.hub.watchdog_tick()

    stalls = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    assert len(stalls) == 1
    assert stalls[0]["detail"] == (
        "all agents idle, nothing queued — unread context: beta 1; "
        "done: —; not done: alpha, beta"
    )


def _stalls(harness: Harness) -> list[dict[str, object]]:
    return [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]


def _quorums(harness: Harness) -> list[dict[str, object]]:
    return [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "quorum"
    ]


async def _finished_floor(harness: Harness) -> float:
    """frank, alpha, beta; both agents say done to frank → session_done.
    Returns the wall time the hub recorded for it."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("alpha", "frank", "fertig", kind="done")
    await harness.say("beta", "frank", "auch fertig", kind="done")
    assert harness.clients["frank"].frames("event")[-1]["event"] == "session_done"
    return harness.clock.wall()


async def test_stall_after_session_done_names_the_time_not_the_marks(
    harness: Harness,
):
    done_at = await _finished_floor(harness)
    harness.clock.advance(120)
    await harness.hub.watchdog_tick()

    stamp = time.strftime("%H:%M:%S", time.localtime(done_at))
    # each done say to frank is overheard context for the other agent —
    # nobody was woken for it, so both hold one unread line
    assert _stalls(harness)[-1]["detail"] == (
        "all agents idle, nothing queued — unread context: alpha 1, beta 1; "
        f"session done at {stamp}"
    )


async def test_an_agent_say_after_session_done_restores_the_done_split(
    harness: Harness,
):
    """An observer's remark after the session does not reopen it; an agent
    speaking again does."""
    await _finished_floor(harness)
    await harness.say("frank", "*", "gut gemacht")  # observer: memory kept
    await harness.state("alpha", "idle")
    await harness.state("beta", "idle")
    harness.clock.advance(120)
    await harness.hub.watchdog_tick()
    assert "session done at" in str(_stalls(harness)[-1]["detail"])

    await harness.say("alpha", "frank", "noch eine frage", kind="question")
    await harness.state("alpha", "idle")
    harness.clock.advance(120)
    await harness.hub.watchdog_tick()
    # the broadcast wake drained both buffers; alpha's question is one new
    # overheard line for beta
    assert _stalls(harness)[-1]["detail"] == (
        "all agents idle, nothing queued — unread context: beta 1; "
        "done: —; not done: alpha, beta"
    )


# ------------------------------------------------------------------ quorum


async def _parley(harness: Harness) -> None:
    """frank, alpha, beta; frank addresses everyone; the clock moves on so
    the answers are strictly later than the question — and so that frank's
    seat counts as unattended when a notification is asserted."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "*", "r1 PARLEY")
    harness.clock.advance(61)


async def _answer(harness: Harness, name: str, text: str) -> None:
    await harness.say(name, "frank", text, kind="claim")
    await harness.state(name, "idle")


async def test_quorum_fires_once_every_addressed_agent_answered(harness: Harness):
    await _parley(harness)
    await _answer(harness, "alpha", "PLEDGE r1: build")
    await harness.hub.watchdog_tick()
    assert _quorums(harness) == []

    await _answer(harness, "beta", "PLEDGE r1: guard")
    await harness.hub.watchdog_tick()
    await harness.hub.watchdog_tick()  # latched: once per observer say
    assert [q["detail"] for q in _quorums(harness)] == [
        "every addressed agent has answered — alpha, beta"
    ]
    assert ("moot", "quorum — alpha, beta answered") in harness.notifier.calls


async def test_quorum_waits_for_every_agent_to_go_idle(harness: Harness):
    await _parley(harness)
    await _answer(harness, "alpha", "PLEDGE r1: build")
    await harness.say("beta", "frank", "PLEDGE r1: guard", kind="claim")  # still busy
    await harness.hub.watchdog_tick()
    assert _quorums(harness) == []

    await harness.state("beta", "idle")
    await harness.hub.watchdog_tick()
    assert len(_quorums(harness)) == 1


async def test_quorum_for_a_direct_say_expects_only_that_agent(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.say("frank", "alpha", "and you?")
    harness.clock.advance(1)
    await _answer(harness, "alpha", "me too")
    await harness.hub.watchdog_tick()

    assert [q["detail"] for q in _quorums(harness)] == [
        "every addressed agent has answered — alpha"
    ]


async def test_quorum_ignores_an_agent_that_left(harness: Harness):
    await _parley(harness)
    await harness.hub.disconnect(harness.clients["beta"])
    await _answer(harness, "alpha", "PLEDGE r1: build")
    await harness.hub.watchdog_tick()

    assert [q["detail"] for q in _quorums(harness)] == [
        "every addressed agent has answered — alpha"
    ]


async def test_quorum_rearms_on_the_next_observer_say(harness: Harness):
    await _parley(harness)
    await _answer(harness, "alpha", "PLEDGE r1: build")
    await _answer(harness, "beta", "PLEDGE r1: guard")
    await harness.hub.watchdog_tick()
    assert len(_quorums(harness)) == 1

    await harness.say("frank", "*", "r1 COMMIT")
    harness.clock.advance(1)
    await harness.hub.watchdog_tick()  # asked again: the old answers do not count
    assert len(_quorums(harness)) == 1
    await _answer(harness, "alpha", "MOVE r1")
    await _answer(harness, "beta", "MOVE r1 too")
    await harness.hub.watchdog_tick()
    assert len(_quorums(harness)) == 2


async def test_observer_to_observer_say_does_not_arm_quorum(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("gerd", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("frank", "gerd", "watch this")
    harness.clock.advance(1)
    await _answer(harness, "alpha", "unprompted")
    await harness.hub.watchdog_tick()

    assert _quorums(harness) == []


async def test_quorum_is_not_fired_by_an_answer_in_the_same_instant(
    harness: Harness,
):
    """Strict `>` on the monotonic clock: an answer stamped at the very
    instant of the question is not an answer to it."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("frank", "alpha", "now")
    await _answer(harness, "alpha", "at once")
    await harness.hub.watchdog_tick()
    assert _quorums(harness) == []

    harness.clock.advance(1)
    await _answer(harness, "alpha", "a moment later")
    await harness.hub.watchdog_tick()
    assert len(_quorums(harness)) == 1


async def test_observer_command_counts_as_activity(harness: Harness):
    """PROTOCOL lists observer commands among the things that keep the
    stall timer quiet: a `/reset` 50 s into the silence restarts the 60 s."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("frank", "alpha", "los")
    await harness.state("alpha", "idle")
    harness.clock.advance(50)
    await harness.cmd("frank", "reset")
    harness.clock.advance(50)
    await harness.hub.watchdog_tick()
    stalls = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    assert stalls == []
    harness.clock.advance(20)
    await harness.hub.watchdog_tick()
    stalls = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "stall"
    ]
    assert len(stalls) == 1


# --------------------------------------------------------------- W1 ok.id


async def test_ok_carries_the_accepted_message_id(harness: Harness):
    """The sender learns the id its message got, so it can cite `#N` and the
    seat can index its own line."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.say("frank", "alpha", "erste")
    ok = harness.clients["frank"].last("ok")
    assert ok is not None and ok["id"] == 1
    assert harness.delivered("alpha")[0]["id"] == 1

    await harness.say("frank", "alpha", "zweite")
    ok = harness.clients["frank"].last("ok")
    assert ok is not None and ok["id"] == 2

    await _say_with_seq(harness, "frank", "niemand", "wohin damit?")
    err = harness.clients["frank"].last("err")
    assert err is not None and "id" not in err


# ------------------------------------------------------------ private says


async def _floor(harness: Harness) -> None:
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.join("gamma")


async def test_private_direct_wakes_only_the_addressee(harness: Harness):
    """A private say is a normal wake for its addressee and nothing at all for
    the third agent — no context copy, no delivery."""
    await _floor(harness)
    await harness.say("alpha", "beta", "geheim", private=True)

    msgs = harness.delivered("beta")
    assert [(m["text"], m["addressing"], m["private"]) for m in msgs] == [
        ("geheim", "direct", True)
    ]
    assert len(harness.hub.participants["gamma"].context) == 0
    assert harness.clients["gamma"].frames("deliver") == []
    assert harness.hub.round == 1


async def test_observer_sees_a_private_say_immediately(harness: Harness):
    await _floor(harness)
    await harness.say("alpha", "beta", "geheim", private=True)

    msgs = harness.delivered("frank")
    assert [(m["text"], m["addressing"], m["private"]) for m in msgs] == [
        ("geheim", "overheard", True)
    ]


async def test_private_say_to_an_observer_is_allowed_and_hidden(harness: Harness):
    """The one way to tell the human something the other agents do not read."""
    await _floor(harness)
    await harness.say("alpha", "frank", "nur für dich", private=True)

    reply = harness.ok_or_err("alpha")
    assert reply is not None and reply["t"] == "ok"
    msgs = harness.delivered("frank")
    assert [(m["text"], m["addressing"], m["private"]) for m in msgs] == [
        ("nur für dich", "direct", True)
    ]
    assert len(harness.hub.participants["beta"].context) == 0
    assert len(harness.hub.participants["gamma"].context) == 0
    assert harness.hub.round == 0


async def test_late_joiner_gets_no_private_backlog(harness: Harness):
    await _floor(harness)
    await harness.say("alpha", "beta", "geheim", private=True)
    await harness.state("beta", "idle")
    await harness.say("alpha", "beta", "öffentlich")

    await harness.join("delta")
    ctx = [m.text for m in harness.hub.participants["delta"].context.items()]
    assert ctx == ["öffentlich"]
    await harness.say("frank", "delta", "wach")
    texts = [m["text"] for m in harness.delivered("delta")]
    assert "öffentlich" in texts and "geheim" not in texts


async def test_late_joiner_gets_the_private_backlog_addressed_to_it(harness: Harness):
    """A name that registers afresh (its dead registration expired) still gets
    the private messages that were addressed to it — as backlog, like the
    public ones — while the third agent gets nothing."""
    await _floor(harness)
    await harness.say("frank", "beta", "für beta allein", private=True)
    await harness.hub.disconnect(harness.clients["beta"])
    harness.hub.dead.clear()  # the 24 h expiry, without waiting for it

    await harness.join("beta")
    ctx = [
        (m.text, m.private, m.addressing)
        for m in harness.hub.participants["beta"].context.items()
    ]
    assert ctx == [("für beta allein", True, "overheard")]
    assert len(harness.hub.participants["gamma"].context) == 0


async def test_restart_reseed_excludes_private_for_agents(tmp_path):
    """The transcript is what a restarted hub reseeds its backlog from, so the
    flag has to survive the round trip: a private message stays out of a
    stranger's backlog and inside its addressee's."""
    from moot.core.clock import FakeClock
    from moot.core.config import Config
    from moot.core.hub import Hub
    from moot.core.notify import CollectingNotifier
    from moot.core.transcript import Transcript

    config = Config(home=tmp_path)
    clock = FakeClock()
    h1 = Harness(tmp_path, config)
    h1.clock = clock
    h1.hub = Hub(
        config, clock, Transcript(config.transcript_dir, clock), CollectingNotifier()
    )
    await h1.join("frank", kind="observer", caps=[])
    await h1.join("alpha")
    await h1.join("beta")
    await h1.say("alpha", "beta", "nur beta", private=True)
    await h1.say("alpha", "beta", "für alle sichtbar")

    h2 = Harness(tmp_path, config)
    h2.clock = clock
    h2.hub = Hub(
        config, clock, Transcript(config.transcript_dir, clock), CollectingNotifier()
    )
    await h2.join("frank", kind="observer", caps=[])
    assert [(m["text"], m.get("private")) for m in h2.delivered("frank")] == [
        ("nur beta", True),
        ("für alle sichtbar", None),
    ]
    await h2.join("gamma")
    assert [m.text for m in h2.hub.participants["gamma"].context.items()] == [
        "für alle sichtbar"
    ]
    await h2.join("beta")  # fresh: a dead registration does not survive a restart
    assert [
        (m.text, m.private) for m in h2.hub.participants["beta"].context.items()
    ] == [("nur beta", True), ("für alle sichtbar", False)]


async def test_private_say_is_transcribed_with_the_flag(harness: Harness):
    await _floor(harness)
    await harness.say("alpha", "beta", "geheim", private=True)
    await harness.say("alpha", "gamma", "offen")

    records = harness.transcript.read_today()
    msgs = [r for r in records if r.get("type") == "msg"]
    assert [(r["text"], r.get("private")) for r in msgs] == [
        ("geheim", True),
        ("offen", None),
    ]
    delivers = [
        r
        for r in records
        if r.get("type") == "deliver" and msgs[0]["id"] in r["msg_ids"]
    ]
    assert sorted(r["to"] for r in delivers) == ["beta", "frank"]
    # the addressee's copy is direct; the observer's copy is tagged from the
    # observer's point of view — overheard — and the record says so
    assert {r["to"]: r["overheard"] for r in delivers} == {
        "beta": [],
        "frank": [msgs[0]["id"]],
    }


async def test_private_under_mute_is_rejected_muted(harness: Harness):
    """Private does not open a side door through mute: agent→agent is
    agent→agent. Agent→observer stays open, private or not."""
    await _floor(harness)
    await harness.cmd("frank", "mute")
    await harness.say("alpha", "beta", "heimlich", private=True)
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_MUTED
    assert harness.hub.participants["beta"].queue == []
    assert "heimlich" not in [m["text"] for m in harness.delivered("beta")]

    await harness.say("alpha", "frank", "an dich, leise", private=True)
    reply = harness.ok_or_err("alpha")
    assert reply is not None and reply["t"] == "ok"
    assert "an dich, leise" in [m["text"] for m in harness.delivered("frank")]


async def test_private_say_survives_a_busy_peer_flush(harness: Harness):
    await _floor(harness)
    await harness.state("beta", "busy")
    await harness.say("alpha", "beta", "geheim", private=True)
    assert harness.delivered("beta") == []

    await harness.state("beta", "idle")
    msgs = harness.delivered("beta")
    assert [(m["text"], m["addressing"], m["private"]) for m in msgs] == [
        ("geheim", "direct", True)
    ]
    assert len(harness.hub.participants["gamma"].context) == 0
    assert harness.clients["gamma"].frames("deliver") == []


async def test_rejected_event_marks_a_private_say(harness: Harness):
    await _floor(harness)
    await harness.cmd("frank", "mute")
    await harness.say("alpha", "beta", "heimlich", private=True)
    await harness.say("alpha", "beta", "laut")

    events = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "rejected"
    ]
    assert [e["code"] for e in events] == [proto.ERR_MUTED, proto.ERR_MUTED]
    assert events[0]["private"] is True and "text" not in events[0]
    assert "private" not in events[1]


async def test_queue_drop_placeholder_names_no_watcher(harness: Harness):
    """The queue-drop placeholder is model-visible: it reports the loss and
    points nowhere — not at a log, a transcript or an observer."""
    harness.hub.config.queue_cap = 1
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.state("alpha", "busy")
    await harness.say("frank", "alpha", "eins")
    await harness.say("frank", "alpha", "zwei")
    await harness.state("alpha", "idle")

    first = harness.delivered("alpha")[0]
    assert first["from"] == proto.SYSTEM_SENDER and first["id"] == 0
    assert first["text"] == "1 addressed messages dropped while you were busy"
    for cue in ("observer", "transcript", "listening", "only you", "log"):
        assert cue not in first["text"].lower(), cue


async def test_private_and_public_texts_do_not_collide_in_dedup(harness: Harness):
    """The dedup key carries the flag: the same words public and then private
    are two messages; a private repeat is still a duplicate."""
    await _floor(harness)
    await harness.say("alpha", "beta", "dasselbe")
    await harness.say("alpha", "beta", "dasselbe", private=True)
    reply = harness.ok_or_err("alpha")
    assert reply is not None and reply["t"] == "ok"

    await harness.say("alpha", "beta", "dasselbe", private=True)
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_DUPLICATE
