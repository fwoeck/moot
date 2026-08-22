"""Lifecycle and command-channel coverage: cmd variants, roster reply,
pre-hello errors, bye, reconnect semantics."""

import pytest

from moot.core import proto
from tests.conftest import Harness


async def test_freeze_resume_reset_variants(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.cmd("frank", "freeze")
    assert harness.hub.frozen is True
    events = [f["event"] for f in harness.clients["frank"].frames("event")]
    assert "frozen" in events

    max_before = harness.hub.max_rounds
    await harness.cmd("frank", "resume")  # without n: just unfreeze
    assert harness.hub.frozen is False
    assert harness.hub.max_rounds == max_before

    harness.hub.round = 7
    await harness.cmd("frank", "reset")
    assert harness.hub.round == 0
    assert harness.hub.frozen is False
    events = harness.clients["frank"].frames("event")
    assert events[-1] == {
        "t": "event",
        "event": "reset",
        "round": 0,
        "from_round": 7,
        "max_rounds": max_before,
    }


async def test_resume_raises_limit(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    before = harness.hub.max_rounds
    await harness.cmd("frank", "resume", {"n": 12})
    assert harness.hub.max_rounds == before + 12


async def test_unknown_cmd_rejected(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.cmd("frank", "explode")
    err = harness.clients["frank"].last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED


async def test_roster_reply(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha", role="Backend")
    await harness.hub.handle(harness.clients["frank"], {"t": "roster"})
    roster = harness.clients["frank"].last("roster")
    assert roster is not None
    assert roster["round"] == 0 and roster["frozen"] is False
    names = {p["name"] for p in roster["peers"]}
    assert names == {"frank", "alpha"}
    alpha = next(p for p in roster["peers"] if p["name"] == "alpha")
    assert alpha["role"] == "Backend" and alpha["state"] == "idle"


async def test_frame_before_hello_rejected(harness: Harness):
    from tests.conftest import FakeClient

    client = FakeClient()
    await harness.hub.handle(
        client, {"t": "say", "to": "*", "kind": "note", "text": "x", "seq": 1}
    )
    err = client.last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED
    assert "hello" in err["detail"]


async def test_unknown_frame_type_rejected(harness: Harness):
    await harness.join("alpha")
    await harness.hub.handle(harness.clients["alpha"], {"t": "warp"})
    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED


async def test_bye_disconnects_and_allows_reclaim(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.hub.handle(harness.clients["alpha"], {"t": "bye"})
    assert "alpha" not in harness.hub.participants
    assert "alpha" in harness.hub.dead
    events = [f["event"] for f in harness.clients["frank"].frames("event")]
    assert "peer_left" in events
    await harness.join("alpha")  # reclaim works
    assert "alpha" in harness.hub.participants


async def test_malformed_frame_keeps_connection(harness: Harness):
    await harness.join("alpha")
    client = harness.clients["alpha"]
    await harness.hub.handle(client, {"t": "say", "to": "*"})  # missing fields
    err = client.last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED
    await harness.say("alpha", "*", "danach geht es noch")  # connection alive
    assert client.last("ok") is not None


async def test_invalid_hello_over_wire_rules(harness: Harness):
    from tests.conftest import FakeClient

    client = FakeClient()
    await harness.hub.handle(
        client, {"t": "hello", "proto": 2, "name": "x", "kind": "opencode"}
    )
    err = client.last("err")
    assert err is not None and err["code"] == proto.ERR_PROTO_MISMATCH


@pytest.mark.parametrize(
    "bad_hello",
    [
        {"t": "hello", "proto": 1, "name": "x", "kind": "opencode", "caps": "notalist"},
        {"t": "hello", "proto": 1, "name": "x", "kind": "opencode", "caps": [1, 2]},
        {"t": "hello", "proto": 1, "name": "x", "kind": "opencode", "role": 42},
        {"t": "hello", "proto": 1, "name": "x", "kind": "bad kind"},
    ],
)
async def test_bad_hello_fields(harness: Harness, bad_hello):
    from tests.conftest import FakeClient

    client = FakeClient()
    await harness.hub.handle(client, bad_hello)
    err = client.last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED


async def test_validation_error_echoes_seq(harness: Harness):
    await harness.join("alpha")
    client = harness.clients["alpha"]

    await harness.hub.handle(
        client, {"t": "say", "to": "*", "kind": "bogus", "text": "x", "seq": 17}
    )
    err = client.last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED
    assert err["seq"] == 17

    await harness.hub.handle(
        client, {"t": "cmd", "cmd": "resume", "args": {"n": "12"}, "seq": 4}
    )
    err = client.last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED
    assert err["seq"] == 4


async def test_resume_rejects_bad_n_instead_of_ignoring(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    before = harness.hub.max_rounds

    await harness.cmd("frank", "resume", {"n": "12"})

    reply = harness.ok_or_err("frank")
    assert reply is not None
    assert reply["t"] == "err" and reply["code"] == proto.ERR_MALFORMED
    assert harness.hub.max_rounds == before
    events = [f["event"] for f in harness.clients["frank"].frames("event")]
    assert "resumed" not in events


async def test_second_hello_on_same_connection_rejected(harness: Harness):
    """A registered connection cannot bind a second name (#1): the hub answers
    err: malformed and keeps both the connection and the first registration."""
    client = await harness.join("alpha")
    await harness.hub.handle(
        client, {"t": "hello", "proto": 1, "name": "beta", "kind": "opencode"}
    )
    err = client.last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED
    assert "alpha" in err["detail"]
    assert "beta" not in harness.hub.participants
    assert client.closed is False

    await harness.say("alpha", "*", "immer noch alpha")
    ok = client.last("ok")
    assert ok is not None


async def test_second_hello_same_name_rejected(harness: Harness):
    """Re-hello with the *own* name on the same connection is a programming
    error, not a name clash: malformed, connection stays open."""
    client = await harness.join("alpha")
    await harness.hub.handle(
        client, {"t": "hello", "proto": 1, "name": "alpha", "kind": "opencode"}
    )
    err = client.last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED
    assert client.closed is False
    assert harness.hub.participants["alpha"].client is client


async def test_hello_reclaims_name_whose_client_is_dead(harness: Harness):
    """A registration whose transport is gone but whose read loop has not
    reaped it yet is reaped by the incoming hello — no name_taken, no lost
    queue."""
    from tests.conftest import DyingClient, FakeClient

    await harness.join("frank", kind="observer", caps=[])
    dying = DyingClient("welcome")
    await harness.join_with(dying, "alpha")
    assert dying.connected is False
    await harness.state("alpha", "busy")
    await harness.say("frank", "alpha", "für den nachfolger")
    assert harness.hub.participants["alpha"].queue

    fresh = await harness.join_with(FakeClient(), "alpha")
    assert fresh.last("welcome") is not None
    assert fresh.last("err") is None
    assert "alpha" not in harness.hub.dead
    assert dying.closed is True
    texts = [m["text"] for m in harness.delivered("alpha")]
    assert "für den nachfolger" in texts


async def test_reclaim_resets_finished_and_blocked(harness: Harness):
    """Reclaim gives the new process a clean slate: no inherited finished mark,
    no inherited blocked bookkeeping."""
    await harness.join("frank", kind="observer", caps=[])
    alpha = await harness.join("alpha")
    await harness.join("beta")  # keeps session_done from firing
    await harness.say("alpha", "*", "fertig", kind="done")
    assert harness.hub.participants["alpha"].finished is True
    await harness.state("alpha", "blocked", detail="permission: bash")

    await harness.hub.disconnect(alpha)
    await harness.join("alpha")

    p = harness.hub.participants["alpha"]
    assert p.finished is False
    assert p.blocked_since is None and p.blocked_detail is None
    await harness.hub.handle(harness.clients["frank"], {"t": "roster"})
    roster = harness.clients["frank"].last("roster")
    assert roster is not None
    entry = next(q for q in roster["peers"] if q["name"] == "alpha")
    assert entry["finished"] is False

    harness.clock.advance(61.0)
    await harness.hub.watchdog_tick()
    blocked = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "blocked"
    ]
    assert blocked == []


async def test_reclaim_with_different_kind_does_not_crash(harness: Harness):
    """An observer registration reclaimed as an agent stays fully functional
    (#9): rate bucket and dedup window are present for every participant."""
    await harness.join("alpha")
    frank = await harness.join("frank", kind="observer", caps=[])
    await harness.hub.disconnect(frank)

    client = await harness.join("frank")  # rejoins as an agent
    assert harness.hub.participants["frank"].is_observer is False
    await harness.say("frank", "alpha", "jetzt als agent")
    assert client.last("err") is None
    assert client.last("ok") is not None


async def test_disconnect_ignores_stale_client_object(harness: Harness):
    """A late disconnect from a superseded client object must not evict the
    live owner of the name (#23)."""
    await harness.join("frank", kind="observer", caps=[])
    stale = await harness.join("alpha")
    await harness.hub.disconnect(stale)
    fresh = await harness.join("alpha")

    await harness.hub.disconnect(stale)

    assert "alpha" in harness.hub.participants
    assert harness.hub.participants["alpha"].client is fresh
    left = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "peer_left"
    ]
    assert len(left) == 1


async def test_joiner_does_not_receive_own_peer_joined(harness: Harness):
    """peer_joined is news for the others, not an echo for the joiner (#41)."""
    frank = await harness.join("frank", kind="observer", caps=[])
    assert [f for f in frank.frames("event") if f["event"] == "peer_joined"] == []

    bea = await harness.join("bea", kind="observer", caps=[])
    joined = [f for f in frank.frames("event") if f["event"] == "peer_joined"]
    assert [f["name"] for f in joined] == ["bea"]
    assert [f for f in bea.frames("event") if f["event"] == "peer_joined"] == []


async def test_bare_resume_after_round_limit_gives_fresh_budget(harness: Harness):
    """A bare resume after a *limit* freeze grants a whole new max_rounds
    budget instead of re-freezing on the next wake (#32)."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    harness.hub.max_rounds = 2
    await harness.say("frank", "alpha", "runde 1")
    await harness.state("alpha", "idle")
    await harness.say("frank", "alpha", "runde 2")
    assert harness.hub.frozen is True and harness.hub.round == 2

    await harness.cmd("frank", "resume")
    assert harness.hub.frozen is False
    assert harness.hub.max_rounds == 2 + harness.config.max_rounds

    await harness.state("alpha", "idle")
    await harness.say("frank", "alpha", "runde 3")
    await harness.state("alpha", "idle")
    await harness.say("frank", "alpha", "runde 4")
    assert harness.hub.round == 4
    assert harness.hub.frozen is False


# ----------------------------------------------------------------- W4 goal


async def test_goal_is_stored_and_announced(harness: Harness):
    """`/goal` records the session goal, tells observers, and buffers one
    context line per agent — without waking anybody or spending a round."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.cmd("frank", "goal", {"text": "find the regression"})

    assert harness.hub.goal == "find the regression"
    events = [
        f for f in harness.clients["frank"].frames("event") if f["event"] == "goal_set"
    ]
    assert len(events) == 1 and events[0]["text"] == "find the regression"
    assert harness.hub.round == 0
    assert harness.clients["alpha"].frames("deliver") == []
    assert len(harness.hub.participants["alpha"].context) == 1


async def test_goal_rides_along_on_the_next_wake(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.cmd("frank", "goal", {"text": "find the regression"})
    await harness.say("frank", "alpha", "los")

    first = harness.delivered("alpha")[0]
    assert first["from"] == proto.SYSTEM_SENDER
    assert first["addressing"] == "overheard"
    assert first["text"] == "moot goal: find the regression"
    assert first["id"] == 0  # not citable, never transcribed


async def test_goal_line_consumes_no_message_id(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.cmd("frank", "goal", {"text": "find the regression"})
    await harness.say("frank", "alpha", "los")

    records = harness.transcript.read_today()
    msgs = [r for r in records if r.get("type") == "msg"]
    assert [r["id"] for r in msgs] == [1]
    delivers = [r for r in records if r.get("type") == "deliver" and r["to"] == "alpha"]
    assert delivers[-1]["msg_ids"] == [0, 1]


async def test_goal_appears_in_welcome_and_roster(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    early = await harness.join("alpha")
    assert "goal" not in (early.last("welcome") or {})
    await harness.roster("frank")
    assert "goal" not in (harness.clients["frank"].last("roster") or {})

    await harness.cmd("frank", "goal", {"text": "find the regression"})
    late = await harness.join("beta")
    welcome = late.last("welcome")
    assert welcome is not None and welcome["goal"] == "find the regression"
    await harness.roster("frank")
    roster = harness.clients["frank"].last("roster")
    assert roster is not None and roster["goal"] == "find the regression"


async def test_goal_file_is_written_0600(harness: Harness):
    import stat

    await harness.join("frank", kind="observer", caps=[])
    await harness.cmd("frank", "goal", {"text": "find the regression"})
    path = harness.config.home / "goal"
    assert path.read_text(encoding="utf-8") == "find the regression\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_goal_from_an_agent_is_forbidden(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.cmd("alpha", "goal", {"text": "meins"})

    err = harness.clients["alpha"].last("err")
    assert err is not None and err["code"] == proto.ERR_FORBIDDEN
    assert harness.hub.goal == ""
    assert not (harness.config.home / "goal").exists()


async def test_empty_goal_is_malformed(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.cmd("frank", "goal", {"text": "   "})

    err = harness.clients["frank"].last("err")
    assert err is not None and err["code"] == proto.ERR_MALFORMED
    assert harness.hub.goal == ""


# ------------------------------------------------------- W5b roster detail


async def test_roster_reports_queue_and_busy_detail(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.state("alpha", "busy")
    harness.clock.advance(250)
    await harness.say("beta", "alpha", "eins")
    await harness.say("frank", "alpha", "zwei")

    await harness.roster("frank")
    roster = harness.clients["frank"].last("roster")
    assert roster is not None
    peers = {p["name"]: p for p in roster["peers"]}
    assert peers["alpha"]["queued"] == 2
    assert peers["alpha"]["queued_from"] == ["beta", "frank"]
    assert peers["alpha"]["busy_for"] == pytest.approx(250.0)
    assert peers["beta"]["queued"] == 0
    assert peers["beta"]["queued_from"] == []
    assert peers["beta"]["busy_for"] == 0.0


async def test_roster_reports_dropped_and_context(tmp_path):
    from moot.core.config import Config

    config = Config(home=tmp_path)
    config.queue_cap = 2
    harness = Harness(tmp_path, config)
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.state("alpha", "busy")
    for i in range(4):
        await harness.say("frank", "alpha", f"an alpha {i}")
    for i in range(3):
        await harness.say("frank", "beta", f"an beta {i}")
        await harness.state("beta", "idle")

    await harness.roster("frank")
    roster = harness.clients["frank"].last("roster")
    assert roster is not None
    alpha = next(p for p in roster["peers"] if p["name"] == "alpha")
    assert alpha["queued"] == 2
    assert alpha["dropped"] == 2
    assert alpha["context"] == 3


async def test_roster_reports_blocked_detail(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.join("alpha")
    await harness.join("beta")
    await harness.state("alpha", "blocked", "permission: bash")

    await harness.roster("frank")
    roster = harness.clients["frank"].last("roster")
    assert roster is not None
    peers = {p["name"]: p for p in roster["peers"]}
    assert peers["alpha"]["state"] == "blocked"
    assert peers["alpha"]["blocked_detail"] == "permission: bash"
    assert peers["beta"]["blocked_detail"] is None


async def test_roster_echoes_seq(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.roster("frank", seq=77)
    roster = harness.clients["frank"].last("roster")
    assert roster is not None and roster["seq"] == 77

    await harness.roster("frank")
    roster = harness.clients["frank"].last("roster")
    assert roster is not None and roster["seq"] is None


async def test_roster_carries_max_rounds_and_goal(harness: Harness):
    await harness.join("frank", kind="observer", caps=[])
    await harness.cmd("frank", "resume", {"n": 6})
    await harness.cmd("frank", "goal", {"text": "find the regression"})

    await harness.roster("frank")
    roster = harness.clients["frank"].last("roster")
    assert roster is not None
    assert roster["max_rounds"] == 30
    assert roster["goal"] == "find the regression"


async def test_cmd_ok_carries_no_message_id(harness: Harness):
    """Only an accepted `say` reports a message id; a command's `ok` has
    nothing to correlate with."""
    await harness.join("frank", kind="observer", caps=[])
    await harness.cmd("frank", "freeze")
    ok = harness.clients["frank"].last("ok")
    assert ok is not None and "id" not in ok
