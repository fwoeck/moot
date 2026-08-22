"""Unit tests: token bucket, dedup window, context buffer, protocol."""

import pytest

from moot.core import proto
from moot.core.bucket import TokenBucket
from moot.core.buffer import ContextBuffer
from moot.core.clock import FakeClock
from moot.core.dedup import DedupWindow
from moot.core.types import Msg


class TestTokenBucket:
    def test_burst_capacity_then_empty(self):
        clock = FakeClock()
        b = TokenBucket(clock, capacity=3, refill_per_second=0.1)
        assert b.take() and b.take() and b.take()
        assert not b.take()

    def test_refill(self):
        clock = FakeClock()
        b = TokenBucket(clock, capacity=3, refill_per_second=0.1)
        b.take()
        b.take()
        b.take()
        clock.advance(10.0)  # exactly one token
        assert b.take()
        assert not b.take()

    def test_refill_capped_at_capacity(self):
        clock = FakeClock()
        b = TokenBucket(clock, capacity=3, refill_per_second=1.0)
        clock.advance(1000.0)
        assert b.take() and b.take() and b.take()
        assert not b.take()

    def test_retry_after_at_boundary(self):
        clock = FakeClock()
        b = TokenBucket(clock, capacity=1, refill_per_second=0.1)
        assert b.retry_after() == 0.0
        b.take()
        assert b.retry_after() == pytest.approx(10.0)
        clock.advance(5.0)
        assert b.retry_after() == pytest.approx(5.0)


class TestDedupWindow:
    def test_repeat_within_window_is_duplicate(self):
        clock = FakeClock()
        d = DedupWindow(clock, window=60.0)
        assert not d.seen("beta", "text")
        assert d.seen("beta", "text")

    def test_different_recipients_do_not_collide(self):
        d = DedupWindow(FakeClock(), window=60.0)
        assert not d.seen("alpha", "text")
        assert not d.seen("beta", "text")

    def test_expiry(self):
        clock = FakeClock()
        d = DedupWindow(clock, window=60.0)
        d.seen("beta", "text")
        clock.advance(61.0)
        assert not d.seen("beta", "text")

    def test_dedup_distinguishes_private(self):
        """The key carries the flag: the same words said publicly and then
        privately are two messages, while a private repeat is a duplicate."""
        d = DedupWindow(FakeClock(), window=60.0)
        assert d.seen("beta", "psst") is False
        assert d.seen("beta", "psst", private=True) is False
        assert d.seen("beta", "psst", private=True) is True
        assert d.seen("beta", "psst") is True


class TestPlaceholderText:
    def test_eviction_placeholder_names_no_watcher(self):
        """The placeholder is model-visible text: it reports the loss and
        points nowhere — not at a log, a transcript or an observer."""
        buf = ContextBuffer(1)
        for i in range(3):
            buf.append(
                Msg(id=i + 1, sender="a", to="b", kind="note", text=f"m{i}", ts=0.0)
            )
        drained = buf.drain()
        assert drained[0].id == 0 and drained[0].text == "2 older messages omitted"
        for cue in ("observer", "transcript", "listening", "only you", "log"):
            assert cue not in drained[0].text.lower(), cue


class TestMsg:
    def test_tag_preserves_private(self):
        m = Msg(
            id=1, sender="alpha", to="beta", kind="note", text="x", ts=0.0, private=True
        )
        assert m.tag("direct").private is True
        assert m.tag("overheard").private is True

    def test_to_wire_carries_private_only_when_set(self):
        public = Msg(id=1, sender="alpha", to="beta", kind="note", text="x", ts=0.0)
        assert "private" not in public.tag("direct").to_wire()
        private = Msg(
            id=2, sender="alpha", to="beta", kind="note", text="x", ts=0.0, private=True
        )
        assert private.tag("direct").to_wire()["private"] is True


class TestContextBuffer:
    def msg(self, i: int) -> Msg:
        return Msg(id=i, sender="a", to="b", kind="note", text=f"m{i}", ts=float(i))

    def test_fifo_eviction_at_cap(self):
        buf = ContextBuffer(cap=3)
        for i in range(5):
            buf.append(self.msg(i))
        out = buf.drain()
        texts = [m.text for m in out]
        assert "m2" in texts and "m4" in texts and "m0" not in texts

    def test_placeholder_reports_evicted_count(self):
        buf = ContextBuffer(cap=2)
        for i in range(4):
            buf.append(self.msg(i))
        out = buf.drain()
        assert out[0].sender == proto.SYSTEM_SENDER
        assert "2 older messages" in out[0].text
        assert [m.text for m in out[1:]] == ["m2", "m3"]

    def test_partial_drain_by_id(self):
        buf = ContextBuffer(cap=5)
        for i in (1, 2, 3):
            buf.append(self.msg(i))
        assert [m.id for m in buf.drain(upto_id=2)] == [1, 2]
        assert [m.id for m in buf.drain()] == [3]
        assert len(buf) == 0

    def test_placeholder_fires_once(self):
        buf = ContextBuffer(cap=1)
        buf.append(self.msg(0))
        buf.append(self.msg(1))
        buf.drain()
        buf.append(self.msg(2))
        out = buf.drain()
        assert [m.text for m in out] == ["m2"]


class TestProto:
    def test_valid_frames(self):
        proto.validate(
            {
                "t": "hello",
                "proto": 1,
                "name": "alpha",
                "kind": "opencode",
                "caps": ["idle-events"],
                "room": "mover",
            }
        )
        proto.validate({"t": "say", "to": "*", "kind": "claim", "text": "x", "seq": 1})
        proto.validate({"t": "state", "state": "blocked"})
        proto.validate({"t": "cmd", "cmd": "resume", "args": {"n": 12}})
        proto.validate({"t": "roster"})
        proto.validate({"t": "bye"})
        proto.validate({"t": "pong"})

    def test_malformed_json(self):
        with pytest.raises(proto.ValidationError) as e:
            proto.parse_line(b"{not json")
        assert e.value.code == proto.ERR_MALFORMED

    def test_non_utf8(self):
        with pytest.raises(proto.ValidationError) as e:
            proto.parse_line(b'{"t":"say","\xff":1}')
        assert e.value.code == proto.ERR_MALFORMED

    def test_unknown_type_is_malformed(self):
        with pytest.raises(proto.ValidationError) as e:
            proto.validate({"t": "warp"})
        assert e.value.code == proto.ERR_MALFORMED

    def test_missing_required_field(self):
        with pytest.raises(proto.ValidationError):
            proto.validate({"t": "say", "to": "*", "kind": "claim"})  # no text/seq

    def test_proto_mismatch(self):
        with pytest.raises(proto.ValidationError) as e:
            proto.validate({"t": "hello", "proto": 99, "name": "a", "kind": "x"})
        assert e.value.code == proto.ERR_PROTO_MISMATCH

    def test_reserved_and_invalid_names(self):
        for bad in ("*", "hub", "system", "System", "HUB", "SYSTEM", "has space", ""):
            with pytest.raises(proto.ValidationError):
                proto.validate({"t": "hello", "proto": 1, "name": bad, "kind": "x"})

    def test_room_validated_but_optional(self):
        proto.validate({"t": "hello", "proto": 1, "name": "a", "kind": "x"})  # no room
        with pytest.raises(proto.ValidationError):
            proto.validate(
                {
                    "t": "hello",
                    "proto": 1,
                    "name": "a",
                    "kind": "x",
                    "room": "has space",
                }
            )

    def test_encode_roundtrip_with_newline_in_text(self):
        frame = {"t": "say", "text": "line1\nline2", "seq": 1}
        encoded = proto.encode(frame)
        assert encoded.count(b"\n") == 1  # only the terminator
        assert proto.parse_line(encoded.rstrip(b"\n"))["text"] == "line1\nline2"

    def test_suggest_name(self):
        taken = {"alpha", "alpha-2"}
        assert proto.suggest_name("alpha", lambda n: n not in taken) == "alpha-3"

    def test_suggest_name_respects_length(self):
        stem = "a" * 32
        out = proto.suggest_name(stem, lambda n: n != stem)
        assert proto.NAME_RE.match(out) and len(out) == 32

        stem31 = "b" * 31
        blocked = {f"{stem31[: 32 - len(f'-{n}')]}-{n}" for n in range(2, 11)}
        out = proto.suggest_name(stem31, lambda n: n not in blocked)
        assert proto.NAME_RE.match(out)
        assert out == "b" * 29 + "-11"

    def test_role_limits(self):
        def hello(role):
            return {"t": "hello", "proto": 1, "name": "a", "kind": "x", "role": role}

        proto.validate(hello("r" * 256))
        with pytest.raises(proto.ValidationError):
            proto.validate(hello("r" * 257))
        with pytest.raises(proto.ValidationError):
            proto.validate(hello("\x1b[31m"))

    def test_text_rejects_control_chars(self):
        def say(text):
            return {"t": "say", "to": "*", "kind": "note", "text": text, "seq": 1}

        proto.validate(say("l1\nl2\tx\r\n"))
        with pytest.raises(proto.ValidationError):
            proto.validate(say("a\x1b[2Jb"))
        with pytest.raises(proto.ValidationError):
            proto.validate(say("\x00"))

    def test_cmd_resume_n_validation(self):
        def cmd(n):
            return {"t": "cmd", "cmd": "resume", "args": {"n": n}}

        proto.validate(cmd(12))
        for bad in ("12", 0, True, 12.5):
            with pytest.raises(proto.ValidationError):
                proto.validate(cmd(bad))

    def test_cmd_seq_must_be_int(self):
        with pytest.raises(proto.ValidationError):
            proto.validate({"t": "cmd", "cmd": "freeze", "seq": "x"})
        proto.validate({"t": "cmd", "cmd": "freeze", "seq": 4})
        proto.validate({"t": "cmd", "cmd": "freeze"})

    def test_state_detail_validation(self):
        def state(detail):
            return {"t": "state", "state": "blocked", "detail": detail}

        proto.validate({"t": "state", "state": "blocked"})  # detail absent
        proto.validate(state(None))
        proto.validate(state("a" * 1024))
        with pytest.raises(proto.ValidationError):
            proto.validate(state(42))
        with pytest.raises(proto.ValidationError):
            proto.validate(state("a" * 1025))
        with pytest.raises(proto.ValidationError):
            proto.validate(state("a\x1bb"))

    def test_say_field_validation(self):
        with pytest.raises(proto.ValidationError):
            proto.validate(
                {"t": "say", "to": "", "kind": "note", "text": "x", "seq": 1}
            )
        with pytest.raises(proto.ValidationError):
            proto.validate(
                {"t": "say", "to": "*", "kind": "rant", "text": "x", "seq": 1}
            )

    def test_private_say_validation(self):
        """`private` is an optional bool; a private broadcast excludes nobody
        and is refused, a private say to a named peer or observer is fine."""
        base = {"t": "say", "to": "beta", "kind": "note", "text": "x", "seq": 1}
        proto.validate({**base, "private": True})
        proto.validate({**base, "private": False})
        proto.validate({**base, "to": "*", "private": False})
        proto.validate(base)
        with pytest.raises(proto.ValidationError) as bad_type:
            proto.validate({**base, "private": "yes"})
        assert bad_type.value.code == proto.ERR_MALFORMED
        with pytest.raises(proto.ValidationError) as broadcast:
            proto.validate({**base, "to": "*", "private": True})
        assert broadcast.value.code == proto.ERR_MALFORMED

    def test_state_validation(self):
        with pytest.raises(proto.ValidationError):
            proto.validate({"t": "state", "state": "sleeping"})

    def test_cmd_validation(self):
        with pytest.raises(proto.ValidationError):
            proto.validate({"t": "cmd", "cmd": "explode"})
        with pytest.raises(proto.ValidationError):
            proto.validate({"t": "cmd", "cmd": "resume", "args": "notadict"})

    def test_frame_must_be_object_with_type(self):
        with pytest.raises(proto.ValidationError):
            proto.parse_line(b"[1,2,3]")
        with pytest.raises(proto.ValidationError):
            proto.validate({"no_type": True})

    def test_goal_cmd_validation(self):
        def goal(args):
            frame = {"t": "cmd", "cmd": "goal"}
            if args is not None:
                frame["args"] = args
            return frame

        proto.validate(goal({"text": "find the regression"}))
        proto.validate(goal({"text": "spalte\tzwei"}))
        for bad in (
            None,
            {},
            {"text": 7},
            {"text": ""},
            {"text": "   "},
            {"text": "a" * 1025},
            {"text": "a\x1bb"},
        ):
            with pytest.raises(proto.ValidationError) as excinfo:
                proto.validate(goal(bad))
            assert excinfo.value.code == proto.ERR_MALFORMED

    def test_control_char_pos(self):
        assert proto.control_char_pos("ab\x01c") == 2
        assert proto.control_char_pos("sauber") is None
        assert proto.control_char_pos("\t\n\r") is None

    def test_opt_seq_is_public(self):
        assert proto.opt_seq({"seq": 17}) == 17
        assert proto.opt_seq({"seq": True}) is None
        assert proto.opt_seq({}) is None
