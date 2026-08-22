"""Property-based invariant tests: random interleavings of say/state
must preserve FIFO per (sender, recipient) pair, the wake invariant (every
deliver carries at least one non-overheard message), no loss, no duplicates,
and queue/context disjointness — across queues and the context buffer.
"""

import asyncio
import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from moot.core.config import Config
from tests.conftest import Harness

AGENTS = ["alpha", "beta", "gamma"]
ALL = ["frank", *AGENTS]


def op_stream():
    return st.lists(
        st.tuples(
            st.sampled_from(ALL),  # sender
            st.sampled_from([*ALL, "*"]),  # recipient
            st.sampled_from(["idle", "busy"]),  # state nudge for agents
            st.booleans(),  # private (dropped for broadcasts)
        ),
        min_size=1,
        max_size=60,
    )


@given(ops=op_stream())
@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_floor_control_invariants(tmp_path, ops):
    asyncio.run(run_scenario(tmp_path, ops))


async def run_scenario(tmp_path, ops):
    # fresh home per hypothesis example — the transcript persists on disk
    case_dir = tmp_path / uuid.uuid4().hex
    config = Config(home=case_dir)
    config.rate_messages = 100_000  # rate limit must not interfere
    config.rate_burst = 100_000
    config.context_cap = 100_000  # eviction is by-design loss, tested
    config.queue_cap = 100_000  # drop-oldest is by-design loss, tested
    config.max_rounds = 100_000  # a round-limit freeze would reject says
    harness = Harness(case_dir, config)  # separately in unit tests
    await harness.join("frank", kind="observer", caps=[])
    for name in AGENTS:
        await harness.join(name)

    counter = 0
    accepted = 0
    for sender, to, nudge, flag in ops:
        counter += 1
        harness.clock.advance(0.1)
        if sender == to:
            continue
        private = flag and to != "*"
        await harness.say(sender, to, f"msg-{counter}", private=private)
        reply = harness.ok_or_err(sender)
        assert reply is not None and reply["t"] in ("ok", "err")
        if reply["t"] == "ok":
            accepted += 1
        # Agents randomly report their state, flushing queues on idle.
        if nudge == "idle" and sender in AGENTS:
            await harness.state(sender, "idle")
        elif nudge == "busy" and sender in AGENTS:
            await harness.state(sender, "busy")
        check_invariants(harness)

    # Final drain: everyone idle, then verify nothing deliverable remains lost.
    for name in AGENTS:
        await harness.state(name, "idle")
    check_invariants(harness)

    # No-loss accounting: every accepted say wrote exactly one transcript msg
    # record, and every routed copy is either delivered, queued, or buffered.
    # Copies share the hub-global msg id, so account per (recipient, id).
    records = [r for r in harness.transcript.read_today() if r.get("type") == "msg"]
    assert len(records) == accepted
    accounted: set[tuple[str, int]] = set()
    for name in AGENTS:
        for m in harness.delivered(name):
            accounted.add((name, m["id"]))
        p = harness.hub.participants[name]
        for m in p.queue:
            accounted.add((name, m.id))
        for m in p.context.items():
            accounted.add((name, m.id))
    expected = 0
    for r in records:
        # one copy per non-sender AGENT (the observer's immediate copies are
        # not part of this accounting); a private say has exactly one agent
        # copy — its addressee's — and none when it went to the observer
        if r.get("private"):
            expected += 1 if r["to"] in AGENTS else 0
        else:
            expected += len([a for a in AGENTS if a != r["from"]])
    assert len(accounted) == expected


def check_invariants(harness: Harness) -> None:
    for name in ALL:
        frames = harness.clients[name].frames("deliver")
        # Wake invariant: every deliver TO AN AGENT has at least one
        # non-overheard msg. Observers are exempt (they receive
        # everything immediately, overheard-only frames included) — the
        # exemption covers this invariant only, the ones below hold for
        # observers too.
        if name != "frank":
            for f in frames:
                assert any(m["addressing"] != "overheard" for m in f["msgs"]), (
                    f"deliver an {name} ohne wake-Nachricht: {f}"
                )
        # No duplicate delivery to the same recipient (system msgs exempt).
        seen: set[int] = set()
        for f in frames:
            for m in f["msgs"]:
                if m["from"] == "system":
                    continue
                assert m["id"] not in seen, f"doppelt zugestellt an {name}: {m}"
                seen.add(m["id"])
        # FIFO per (sender → this recipient): delivered ids ascend per pair.
        per_pair: dict[str, list[int]] = {}
        for f in frames:
            for m in f["msgs"]:
                per_pair.setdefault(m["from"], []).append(m["id"])
        for sender, ids in per_pair.items():
            assert ids == sorted(ids), f"FIFO verletzt {sender} → {name}: {ids}"

    # Disjointness: a message is in queue or context, never both.
    for name in AGENTS:
        p = harness.hub.participants[name]
        queue_ids = {m.id for m in p.queue}
        ctx_ids = {m.id for m in p.context.items()}
        assert not (queue_ids & ctx_ids), f"{name}: Nachricht in queue UND context"

    # Privacy: a private message reaches its addressee (and observers) only —
    # never another agent's queue, context buffer or deliveries.
    for name in AGENTS:
        p = harness.hub.participants[name]
        for held in [*p.queue, *p.context.items()]:
            assert not (held.private and held.to != name), (
                f"{name} hält eine private Nachricht für {held.to}"
            )
        for f in harness.clients[name].frames("deliver"):
            for m in f["msgs"]:
                assert not (m.get("private") and m["to"] != name), (
                    f"private Nachricht für {m['to']} an {name} zugestellt"
                )
