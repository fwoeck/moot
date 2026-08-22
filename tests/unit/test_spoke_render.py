"""render() against the shared fixture (tests/fixtures/render_cases.json).

The same file pins the OpenCode plugin's TypeScript renderer, so a change
here is a change to both spokes.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from moot.spoke.render import message_head, message_line, rejoined_line, render

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "render_cases.json"
CASES: list[dict[str, Any]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_fixture_covers_every_frame_type():
    assert {case["frame"]["t"] for case in CASES} >= {
        "welcome",
        "deliver",
        "event",
        "err",
        "ok",
        "roster",
    }


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_render_case(case: dict[str, Any]):
    assert render(case["frame"]) == case["lines"]


def test_message_line_and_head():
    assert message_head(None, 0) == "[context]"
    assert message_head(None, 42) == "[context #42]"
    assert message_head(3, 0) == "[r3]"
    assert message_head(3, 42) == "[r3 #42]"
    assert (
        message_line(3, 42, "beta", "alpha", "question", "which migration ran last?")
        == "[r3 #42] beta → alpha · question: which migration ran last?"
    )
    assert (
        message_line(None, 0, "system", "*", "note", "2 older messages omitted")
        == "[context] system → * · note: 2 older messages omitted"
    )
    assert (
        message_line(3, 42, "alpha", "beta", "claim", "psst", private=True)
        == "[r3 #42] alpha → beta · private · claim: psst"
    )


def test_rejoined_line():
    """A stream that comes back says so in its own words: the floor has moved
    on, so the round and the peers are the news, not the joiner's kind."""
    frame = {
        "t": "welcome",
        "name": "alpha",
        "kind": "claude-code",
        "round": 7,
        "peers": [
            {"name": "beta", "kind": "opencode", "role": "refuter", "state": "busy"},
            {"name": "frank", "kind": "observer", "role": "", "state": "idle"},
        ],
        "limits": {"max_rounds": 24},
    }
    assert rejoined_line(frame) == (
        "[moot] rejoined as alpha · round 7/24"
        " · peers: beta (opencode, refuter, busy), frank (observer, idle)"
    )
    frame["peers"] = []
    del frame["limits"]
    assert rejoined_line(frame) == "[moot] rejoined as alpha · round 7 · peers: none"


def test_text_is_verbatim_including_newlines():
    # No `id` on the message, so the head carries no `#tag` — the same
    # suppression the fixture pins for the hub's id-0 placeholders.
    frame = {
        "t": "deliver",
        "round": 1,
        "msgs": [
            {
                "from": "beta",
                "to": "alpha",
                "addressing": "direct",
                "kind": "result",
                "text": "a\n\tb  c",
            }
        ],
    }
    assert render(frame) == ["[r1] beta → alpha · result: a\n\tb  c"]


def test_unknown_frame_type_renders_nothing():
    assert render({"t": "banana"}) == []


def test_malformed_deliver_raises():
    with pytest.raises(ValueError, match="msgs list"):
        render({"t": "deliver", "round": 1})


def test_malformed_roster_raises():
    with pytest.raises(ValueError, match="peers list"):
        render({"t": "roster", "round": 1})
