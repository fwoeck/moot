"""`moot doctor`: every check becomes a row and the exit code stays 0.

`Path.home()` and `$HOME` are redirected for every test — the real ones hold a
live `~/.moot`, `~/.claude` and `~/.config/opencode` — and the two checks that
shell out (`shutil.which`, `ps`) are stubbed, so no test starts a process.
"""

import os
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from moot.cli import doctor
from moot.cli.doctor import run_doctor
from moot.spoke.home import write_current

LABELS = (
    "moot on PATH",
    "editable install",
    "claude skill",
    "bun",
    "opencode plugin",
    "AF_UNIX headroom",
    "~/.moot/current",
    "hub",
    "ctl sockets",
    "orphan processes",
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("HOME", str(home))  # Path.expanduser() reads $HOME
    monkeypatch.delenv("MOOT_HOME", raising=False)
    return home


@pytest.fixture
def no_subprocesses(monkeypatch) -> None:
    """`which` finds nothing (so the bun probe never runs) and `ps` is empty."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(doctor, "_ps_lines", lambda: [])


@pytest.fixture
def short_home() -> Iterator[Path]:
    # macOS limits AF_UNIX paths to 104 chars; pytest's tmp_path exceeds that.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        yield Path(d)


def _rows(out: str) -> dict[str, tuple[bool, str]]:
    rows: dict[str, tuple[bool, str]] = {}
    for line in out.splitlines():
        if not line.startswith(("pass  ", "FAIL  ")):
            continue
        label, _, detail = line[6:].partition("  ")
        rows[label.strip()] = (line.startswith("pass"), detail.strip())
    return rows


def _run(home: Path, capsys) -> dict[str, tuple[bool, str]]:
    assert run_doctor(home, roster=False) == 0
    return _rows(capsys.readouterr().out)


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "opencode").mkdir(parents=True)
    (repo / "skills" / "moot").mkdir(parents=True)
    (repo / "opencode" / "moot.ts").touch()
    (repo / "skills" / "moot" / "SKILL.md").touch()
    return repo


def test_every_row_is_reported_and_exit_is_zero(
    fake_home: Path, tmp_path: Path, no_subprocesses, capsys
):
    rows = _run(tmp_path / "session", capsys)

    assert list(rows) == list(LABELS)


def test_af_unix_headroom_fails_for_a_long_home(
    fake_home: Path, no_subprocesses, capsys
):
    """`<home>/ctl/<32-char session>.sock` must fit in 103 bytes, and the path
    adds 46 characters to the home — so 57 fits and 58 does not."""
    fits = Path("/tmp/" + "x" * 52)
    assert len(str(fits)) == 57

    ok, detail = _run(fits, capsys)["AF_UNIX headroom"]
    assert ok and detail.startswith("103/103 bytes")

    ok, detail = _run(Path(str(fits) + "x"), capsys)["AF_UNIX headroom"]
    assert not ok and detail.startswith("104/103 bytes")


def test_hub_row_detects_a_live_socket(
    fake_home: Path, short_home: Path, no_subprocesses, capsys
):
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(short_home / "hub.sock"))
    sock.listen(1)
    try:
        ok, detail = _run(short_home, capsys)["hub"]
        assert ok and "answers" in detail
    finally:
        sock.close()
    (short_home / "hub.sock").unlink()

    ok, detail = _run(short_home, capsys)["hub"]
    assert not ok and "no hub at" in detail


def test_current_row_handles_absent_stale_and_live(
    fake_home: Path, short_home: Path, no_subprocesses, capsys
):
    ok, detail = _run(short_home, capsys)["~/.moot/current"]
    assert ok and "is absent" in detail

    write_current(short_home)
    ok, detail = _run(short_home, capsys)["~/.moot/current"]
    assert not ok and "no hub there" in detail

    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(short_home / "hub.sock"))
    sock.listen(1)
    try:
        ok, detail = _run(short_home, capsys)["~/.moot/current"]
        assert ok and "hub answers" in detail
    finally:
        sock.close()


def test_skill_and_plugin_rows_need_a_symlink_to_this_repo(
    fake_home: Path, tmp_path: Path, no_subprocesses, monkeypatch, capsys
):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(doctor, "_repo_root", lambda: repo)
    skill = fake_home / ".claude" / "skills" / "moot"
    plugin = fake_home / ".config" / "opencode" / "plugins" / "moot.ts"
    skill.parent.mkdir(parents=True)
    plugin.parent.mkdir(parents=True)

    rows = _run(tmp_path / "session", capsys)
    assert rows["claude skill"] == (False, f"{skill} is missing")
    assert rows["opencode plugin"][0] is False

    skill.mkdir()  # a copy, not a link: edits in the repo would not be seen
    plugin.symlink_to(tmp_path / "elsewhere.ts")
    rows = _run(tmp_path / "session", capsys)
    assert rows["claude skill"] == (False, f"{skill} is not a symlink")
    assert not rows["opencode plugin"][0]
    assert "not" in rows["opencode plugin"][1]

    skill.rmdir()
    skill.symlink_to(repo / "skills" / "moot")
    plugin.unlink()
    plugin.symlink_to(repo / "opencode" / "moot.ts")
    rows = _run(tmp_path / "session", capsys)
    assert rows["claude skill"][0] and rows["opencode plugin"][0]


def test_orphan_scan_skips_this_pid_and_this_home(
    fake_home: Path, tmp_path: Path, monkeypatch, capsys
):
    home = tmp_path / "session"
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        doctor,
        "_ps_lines",
        lambda: [
            " 6247 /Users/x/agentbus/.venv/bin/python3 -c from moot.cli import"
            " main; main() serve --home /tmp/moot-SOhQ --no-notify",
            f" {os.getpid()} /Users/x/.local/bin/moot serve --home /tmp/moot-self",
            f" 4242 /Users/x/.local/bin/moot stream --home {home} --name alpha",
            " 4243 /usr/sbin/syslogd",
        ],
    )

    ok, detail = _run(home, capsys)["orphan processes"]

    assert not ok
    assert detail == "pid 6247 serve on /tmp/moot-SOhQ"


def test_editable_install_row_fails_without_the_repo_files(
    fake_home: Path, tmp_path: Path, no_subprocesses, monkeypatch, capsys
):
    monkeypatch.setattr(doctor, "_repo_root", lambda: tmp_path / "wheel")

    ok, detail = _run(tmp_path / "session", capsys)["editable install"]

    assert not ok
    assert "is not a checkout" in detail


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (subprocess.CompletedProcess([], 0), (True, "imports")),
        (subprocess.CompletedProcess([], 1), (False, "does not import")),
        (OSError("bun vanished"), (False, "probe failed")),
    ],
)
def test_bun_row_reports_the_plugin_import_probe(
    fake_home: Path, tmp_path: Path, monkeypatch, capsys, outcome, expected
):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(doctor, "_repo_root", lambda: repo)
    monkeypatch.setattr(doctor, "_ps_lines", lambda: [])
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")

    def probe(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(doctor.subprocess, "run", probe)

    ok, detail = _run(tmp_path / "session", capsys)["bun"]

    assert (ok, expected[1] in detail) == (expected[0], True)


def test_ctl_socket_rows_split_live_from_dead(
    fake_home: Path, short_home: Path, no_subprocesses, capsys
):
    ctl = short_home / "ctl"
    ctl.mkdir()
    (ctl / "stale.sock").touch()  # a dead `moot stream` leaves the file behind
    live = socket.socket(socket.AF_UNIX)
    live.bind(str(ctl / "live.sock"))
    live.listen(1)
    try:
        ok, detail = _run(short_home, capsys)["ctl sockets"]
    finally:
        live.close()

    assert not ok
    assert detail == "live (live), stale (dead)"


def test_roster_failure_becomes_a_row_and_restores_the_socket_timeout(
    fake_home: Path, short_home: Path, no_subprocesses, capsys
):
    before = socket.getdefaulttimeout()

    assert run_doctor(short_home, roster=True) == 0

    ok, detail = _rows(capsys.readouterr().out)["roster"]
    assert not ok
    assert "could not read" in detail
    assert socket.getdefaulttimeout() == before
