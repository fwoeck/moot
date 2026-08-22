"""Argument handling that never reaches a socket."""

import argparse
import sys
from pathlib import Path

import pytest

from moot import cli
from moot.cli import _run_brief, _run_observe, _run_say, _run_serve, main
from moot.core import server
from moot.core.config import Config
from moot.spoke import observer


def test_serve_expands_a_tilde_home(monkeypatch):
    """Every client expands `--home` (spoke/home.py); a quoted `~/…` would
    otherwise bind the hub next to the cwd while the spokes look in $HOME."""
    seen: list[Path] = []
    monkeypatch.setattr(server, "serve_main", lambda config: seen.append(config.home))
    args = argparse.Namespace(
        no_notify=True, home="~/moot-test", transcripts=None, max_rounds=None
    )
    assert _run_serve(args) == 0
    assert seen == [Path.home() / "moot-test"]


def test_serve_without_a_home_keeps_the_configured_default(monkeypatch):
    seen: list[Path] = []
    monkeypatch.setattr(server, "serve_main", lambda config: seen.append(config.home))
    args = argparse.Namespace(
        no_notify=False, home=None, transcripts=None, max_rounds=None
    )
    assert _run_serve(args) == 0
    assert seen == [Config().home]


def test_observe_passes_the_no_tui_flag_through(monkeypatch):
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(observer, "run_observer", lambda *a: seen.append(a) or 0)
    args = argparse.Namespace(
        home="/tmp/moot-x",
        name="frank",
        full=False,
        width=None,
        no_color=True,
        no_tui=True,
    )
    assert _run_observe(args) == 0
    assert seen[0][:2] == (Path("/tmp/moot-x"), "frank")
    assert seen[0][-1] is True  # no_tui is the last positional argument


def _serve_args(**overrides) -> argparse.Namespace:
    args = {"no_notify": True, "home": None, "transcripts": None, "max_rounds": None}
    args.update(overrides)
    return argparse.Namespace(**args)


def _captured_config(monkeypatch, **overrides) -> Config:
    seen: list[Config] = []
    monkeypatch.setattr(server, "serve_main", lambda config: seen.append(config))
    assert _run_serve(_serve_args(**overrides)) == 0
    return seen[0]


def test_serve_expands_a_tilde_transcripts_dir(monkeypatch):
    """`--transcripts` is expanded like `--home`: a quoted `~/…` would put the
    transcripts in a directory named `~` next to the cwd."""
    config = _captured_config(monkeypatch, transcripts="~/t")
    assert config.transcripts == Path.home() / "t"


def test_serve_passes_max_rounds_through(monkeypatch):
    assert _captured_config(monkeypatch, max_rounds=12).max_rounds == 12


def test_serve_without_max_rounds_keeps_the_default(monkeypatch):
    assert _captured_config(monkeypatch).max_rounds == Config().max_rounds


def test_max_rounds_zero_is_an_argparse_error(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["moot", "serve", "--max-rounds", "0"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2


def test_max_rounds_not_a_number_is_an_argparse_error(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["moot", "serve", "--max-rounds", "viele"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2


# ----------------------------------------------------------------------- say


def _say_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    args = {
        "home": str(tmp_path),
        "kind": "note",
        "session": "s",
        "private": False,
        "words": ["@beta", "hallo"],
    }
    args.update(overrides)
    return argparse.Namespace(**args)


def _replies(monkeypatch, reply: dict[str, object]) -> list[dict[str, object]]:
    sent: list[dict[str, object]] = []

    def fake_ctl_call(path, frame, timeout=10.0):
        sent.append(frame)
        return reply

    monkeypatch.setattr(cli, "ctl_call", fake_ctl_call)
    return sent


def test_say_line_shows_the_message_id(monkeypatch, capsys, tmp_path: Path):
    """The id is what a peer quotes back as `#47`, so the sender has to see
    the one the hub assigned."""
    _replies(monkeypatch, {"t": "ok", "id": 47, "queued": 0})
    assert _run_say(_say_args(tmp_path)) == 0
    assert capsys.readouterr().out.strip() == "ok → beta · #47"


def test_say_line_shows_the_id_and_the_queue(monkeypatch, capsys, tmp_path: Path):
    _replies(monkeypatch, {"t": "ok", "id": 47, "queued": 2})
    assert _run_say(_say_args(tmp_path)) == 0
    out = capsys.readouterr().out.strip()
    assert out == "ok → beta · #47 · queued at 2 busy peer(s)"


@pytest.mark.parametrize("mid", [None, 0, True])
def test_say_line_without_an_id(monkeypatch, capsys, tmp_path: Path, mid: object):
    """An `ok` without a usable id still confirms the send; `#0` and `#True`
    are not message ids and are never printed."""
    reply: dict[str, object] = {"t": "ok", "queued": 0}
    if mid is not None:
        reply["id"] = mid
    _replies(monkeypatch, reply)
    assert _run_say(_say_args(tmp_path)) == 0
    assert capsys.readouterr().out.strip() == "ok → beta"


def test_say_private_flag_is_forwarded(monkeypatch, capsys, tmp_path: Path):
    """`--private` rides on the ctl frame as the wire field, and the ok line
    says so — the sender should see what it sent."""
    sent = _replies(monkeypatch, {"t": "ok", "id": 47, "queued": 0})
    assert _run_say(_say_args(tmp_path, private=True)) == 0
    assert sent == [
        {"t": "say", "to": "beta", "kind": "note", "text": "hallo", "private": True}
    ]
    assert capsys.readouterr().out.strip() == "ok → beta · #47 · private"


def test_say_public_frame_carries_no_private_key(monkeypatch, tmp_path: Path):
    sent = _replies(monkeypatch, {"t": "ok", "id": 47, "queued": 0})
    assert _run_say(_say_args(tmp_path)) == 0
    assert "private" not in sent[0]


def test_say_private_without_a_name_is_refused_locally(
    monkeypatch, capsys, tmp_path: Path
):
    """A private broadcast excludes nobody; the hub would say `malformed`, the
    CLI says why before anything reaches the stream."""
    sent = _replies(monkeypatch, {"t": "ok", "id": 1, "queued": 0})
    assert _run_say(_say_args(tmp_path, private=True, words=["hallo"])) == 1
    assert sent == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "moot: --private needs @NAME"


def test_say_error_shows_the_retry_wait(monkeypatch, capsys, tmp_path: Path):
    _replies(
        monkeypatch,
        {
            "t": "err",
            "code": "rate_limited",
            "detail": "rate limit",
            "retry_after": 8.2,
        },
    )
    assert _run_say(_say_args(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "err rate_limited: rate limit · retry in 8.2s"


def test_say_error_without_a_retry_wait(monkeypatch, capsys, tmp_path: Path):
    _replies(monkeypatch, {"t": "err", "code": "frozen", "detail": "the floor is held"})
    assert _run_say(_say_args(tmp_path)) == 1
    assert capsys.readouterr().err.strip() == "err frozen: the floor is held"


def test_say_no_stream_message(monkeypatch, capsys, tmp_path: Path):
    """`no_stream` is the one code with a line of its own: the fix is to start
    a stream, not to read a socket path."""
    _replies(monkeypatch, {"t": "err", "code": "no_stream", "detail": "no socket"})
    assert _run_say(_say_args(tmp_path)) == 1
    assert capsys.readouterr().err.strip() == "moot: no moot stream for session s"


# --------------------------------------------------------------------- brief


def _brief_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    args = {
        "runtime": "claude-code",
        "name": None,
        "role": "",
        "home": str(tmp_path),
        "session": "s",
    }
    args.update(overrides)
    return argparse.Namespace(**args)


def test_brief_with_an_explicit_name_never_touches_the_ctl_socket(
    monkeypatch, capsys, tmp_path: Path
):
    def explode(*args, **kwargs):
        raise AssertionError("--name is the answer; nothing to ask")

    monkeypatch.setattr(cli, "ctl_call", explode)
    assert _run_brief(_brief_args(tmp_path, name="alpha", role="prover")) == 0
    assert "You are `alpha` (role: prover)" in capsys.readouterr().out


def test_brief_resolves_its_name_through_whoami(monkeypatch, capsys, tmp_path: Path):
    """A compacted session knows its session id and nothing else; the stream
    that holds its registration knows the rest."""
    seen: list[tuple[dict[str, object], float]] = []

    def fake_ctl_call(path, frame, timeout=10.0):
        seen.append((frame, timeout))
        return {"t": "ok", "name": "beta", "kind": "claude-code", "role": "tests"}

    monkeypatch.setattr(cli, "ctl_call", fake_ctl_call)
    assert _run_brief(_brief_args(tmp_path)) == 0
    assert seen == [({"t": "whoami"}, 1.5)]
    out = capsys.readouterr().out
    assert "You are `beta` (role: tests)" in out
    assert "your floor connection is gone" not in out


def test_brief_prefers_an_explicit_role(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(
        cli,
        "ctl_call",
        lambda path, frame, timeout=10.0: {"t": "ok", "name": "beta", "role": "tests"},
    )
    assert _run_brief(_brief_args(tmp_path, role="refuter")) == 0
    assert "You are `beta` (role: refuter)" in capsys.readouterr().out


def test_brief_survives_a_dead_stream(monkeypatch, capsys, tmp_path: Path):
    """The rules are what the model came for: a missing stream costs the name
    and adds one line, it never costs the brief."""
    monkeypatch.setattr(
        cli,
        "ctl_call",
        lambda path, frame, timeout=10.0: {
            "t": "err",
            "code": "no_stream",
            "detail": "no socket",
        },
    )
    assert _run_brief(_brief_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "You are a participant" in out
    assert "your floor connection is gone" in out


def test_brief_survives_an_exploding_ctl_call(monkeypatch, capsys, tmp_path: Path):
    def explode(*args, **kwargs):
        raise ConnectionError("closed without a reply")

    monkeypatch.setattr(cli, "ctl_call", explode)
    assert _run_brief(_brief_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "You are a participant" in out
    assert "your floor connection is gone" in out


def test_brief_survives_an_unresolvable_session(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        cli, "ctl_call", lambda *a, **k: pytest.fail("no session to ask about")
    )
    assert _run_brief(_brief_args(tmp_path, session=None)) == 0
    out = capsys.readouterr().out
    assert "You are a participant" in out
    assert "your floor connection is gone" in out


def test_brief_carries_the_goal_file(capsys, tmp_path: Path):
    (tmp_path / "goal").write_text("decide the cache key\n", encoding="utf-8")
    assert _run_brief(_brief_args(tmp_path, name="beta")) == 0
    assert "- Session goal: decide the cache key" in capsys.readouterr().out


def test_brief_without_a_goal_file(capsys, tmp_path: Path):
    assert _run_brief(_brief_args(tmp_path, name="beta")) == 0
    assert "Session goal" not in capsys.readouterr().out
