"""The operating rules: one source for skill body, re-injection and plugin."""

import pytest

from moot.core import proto
from moot.spoke.brief import brief


@pytest.mark.parametrize("runtime", ["claude-code", "opencode"])
def test_brief_stays_within_its_budget(runtime: str):
    """Measured: 36 lines for claude-code, 39 for opencode (37 / 40 with a
    session goal) — the private rules (two lines each, plus one send hint),
    the reworded frozen rule and the unknown_recipient recovery rule (two
    lines) are in that count. The cap is the larger measurement plus two, so
    one more rule line still fits the goal-carrying brief; a second costs a
    decision."""
    assert len(brief(runtime, "alpha").splitlines()) <= 42
    assert len(brief(runtime, "alpha", "refuter", "ship it").splitlines()) <= 42


@pytest.mark.parametrize("runtime", ["claude-code", "opencode"])
def test_brief_carries_every_rule(runtime: str):
    """The frozen and the file rules are worded without naming who paused the
    floor or that a record exists — deliberately (see
    test_brief_names_no_watcher); the behaviour they ask for is unchanged."""
    text = brief(runtime, "alpha")
    assert "`alpha`" in text
    assert "Answer only when a message is addressed to you" in text
    assert "[context]" in text and "never answer them" in text
    assert "No unsolicited follow-ups" in text
    assert "@name" in text and "everyone with `*`" in text
    for kind in proto.SAY_KINDS:
        assert kind in text
    assert "`done` kind when your part is complete" in text
    # the form a sender actually sees: a frozen err carries the seq of its
    # own `say`, so the stream hands it to `moot say`, never to render()
    assert "`err frozen: …`" in text and "the floor is paused" in text
    assert "stop sending and wait" in text
    assert "[err] frozen" not in text
    assert "rate_limited" in text and "never retry in a loop" in text
    assert "unknown_recipient" in text and "never invent a name" in text
    assert "never read moot's" in text and "own files" in text
    assert "· private" in text and "no other agent" in text
    assert "not sticky" in text
    assert "[rN #id]" in text
    assert "`#N`" in text
    assert "what you assert and what it rests on" in text
    assert "what still survives it" in text
    assert "what came out of running or checking something" in text
    assert "or say that you did not check" in text
    assert "names what it retracts" in text


@pytest.mark.parametrize("runtime", ["claude-code", "opencode"])
def test_brief_names_no_watcher(runtime: str):
    """What a model reads on the floor states only what is true about agents:
    it never says who else reads a message (private or not), and never that a
    record is kept. Not a denial either — the uncertainty is the point."""
    text = brief(runtime, "alpha", "refuter", "ship it").lower()
    for cue in ("observer", "transcript", "listening", "only you"):
        assert cue not in text, cue


@pytest.mark.parametrize("runtime", ["claude-code", "opencode"])
def test_brief_never_promises_observer_only_frames(runtime: str):
    """`event` frames go to observers only (PROTOCOL.md), never to an agent."""
    assert "[event]" not in brief(runtime, "alpha")


def test_send_line_is_runtime_specific():
    assert "moot say @beta" in brief("claude-code", "alpha")
    assert "moot_say" not in brief("claude-code", "alpha")
    assert "moot_say({to:" in brief("opencode", "beta")
    assert "moot say" not in brief("opencode", "beta")


def test_opencode_brief_guards_against_skill_requests():
    # OpenCode has no skill tool; an instruction like "activate the skill X"
    # otherwise sends the model chasing a tool that is not in its list
    text = brief("opencode", "beta")
    assert "no skill system" in text
    assert "never attempt a tool that is not in" in text
    assert "skill" not in brief("claude-code", "alpha")  # Claude Code has skills


def test_brief_names_the_role():
    assert "You are `beta` (role: refuter)" in brief("claude-code", "beta", "refuter")
    assert "role:" not in brief("claude-code", "beta")


def test_brief_carries_the_goal():
    text = brief("claude-code", "beta", "refuter", "decide the cache key")
    assert "- Session goal: decide the cache key" in text
    assert "Session goal" not in brief("claude-code", "beta", "refuter")


def test_brief_without_a_name():
    text = brief("claude-code", None)
    assert "You are a participant" in text
    assert "``" not in text


def test_brief_without_a_name_has_no_role_clause():
    """A name-less brief is the compacted-session fallback: there is nobody to
    give a role to, and an empty backtick pair would be worse than silence."""
    text = brief("claude-code", None, "refuter")
    assert "You are a participant" in text
    assert "role:" not in text
    assert "``" not in text


def test_unknown_runtime():
    with pytest.raises(ValueError, match="unknown runtime"):
        brief("gemini", "alpha")
