"""`moot doctor`: read-only checks on the install and one state directory.

Every check becomes a row — `pass`/`FAIL`, a label, a detail, and a fix hint
for a failing row — and the command always exits 0: doctor reports, it does
not decide. That is why this module is the one place that converts failures
(a missing `ps`, a probe that times out, a hub that refuses the roster) into
output instead of letting them propagate.

`--roster` is the only check that touches the floor: it joins as an observer,
which resets and re-arms the stall timer and leaves a dead registration behind
(see docs/OPERATIONS.md, Troubleshooting).
"""

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import moot
from moot.spoke.conn import HubError, connect
from moot.spoke.ctl import ctl_path
from moot.spoke.home import current_file, hub_alive
from moot.spoke.render import render

# macOS refuses to bind an AF_UNIX path of 104 bytes; 103 is the budget, and a
# session name is at most 32 characters (proto.NAME_RE).
_AF_UNIX_BUDGET = 103
_PROBE_NAME = "s" * 36

_PS = ("ps", "-axww", "-o", "pid=,command=")
_PS_TIMEOUT = 5.0
_BUN_TIMEOUT = 30.0
_PROBE_TIMEOUT = 0.5
_ROSTER_TIMEOUT = 3.0


@dataclass(frozen=True)
class Row:
    ok: bool
    label: str
    detail: str
    fix: str = ""


def _repo_root() -> Path:
    """The checkout this `moot` was installed from — `src/moot/__init__.py`
    two levels up. A non-editable wheel lands in `site-packages`, which the
    editable-install row detects by the repo files that are then missing."""
    return Path(moot.__file__).resolve().parents[2]


def _row_path() -> Row:
    found = shutil.which("moot")
    if found is None:
        return Row(
            False,
            "moot on PATH",
            "not found — hooks and the OpenCode plugin cannot run it",
            f"uv tool install --editable {_repo_root()}",
        )
    return Row(True, "moot on PATH", found)


def _row_editable(repo: Path) -> Row:
    missing = [
        str(p)
        for p in (repo / "opencode" / "moot.ts", repo / "skills" / "moot" / "SKILL.md")
        if not p.exists()
    ]
    if missing:
        return Row(
            False,
            "editable install",
            f"{repo} is not a checkout ({', '.join(missing)} missing)",
            "uv tool install --editable <repo>",
        )
    return Row(True, "editable install", str(repo))


def _row_symlink(label: str, link: Path, target: Path) -> Row:
    fix = f"ln -s {target} {link}"
    if not link.is_symlink():
        # Path.exists() follows symlinks, so this order matters.
        state = "is not a symlink" if link.exists() else "is missing"
        return Row(False, label, f"{link} {state}", fix)
    resolved = link.resolve()
    if resolved != target.resolve():
        return Row(False, label, f"{link} → {resolved}, not {target}", fix)
    return Row(True, label, f"{link} → {target}")


def _row_bun(repo: Path) -> Row:
    found = shutil.which("bun")
    fix = f"cd {repo}/opencode && bun install"
    if found is None:
        return Row(False, "bun", "not found — the OpenCode spoke needs it", fix)
    try:
        done = subprocess.run(
            [found, "-e", 'import("@opencode-ai/plugin")'],
            cwd=repo / "opencode",
            capture_output=True,
            text=True,
            timeout=_BUN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Row(False, "bun", f"{found} — probe failed: {exc}", fix)
    if done.returncode != 0:
        return Row(False, "bun", f"{found} — @opencode-ai/plugin does not import", fix)
    return Row(True, "bun", f"{found} — @opencode-ai/plugin imports")


def _row_af_unix(home: Path) -> Row:
    used = len(str(ctl_path(home, _PROBE_NAME)))
    return Row(
        used <= _AF_UNIX_BUDGET,
        "AF_UNIX headroom",
        f"{used}/{_AF_UNIX_BUDGET} bytes for the longest control socket",
        ""
        if used <= _AF_UNIX_BUDGET
        else "use a shorter --home, e.g. /tmp/moot-<stamp>",
    )


def _row_current() -> Row:
    current = current_file()
    label = "~/.moot/current"
    if not current.exists():
        return Row(True, label, f"{current} is absent — spokes fall back to ~/.moot")
    recorded = current.read_text(encoding="utf-8").strip()
    if not recorded:
        return Row(False, label, f"{current} is empty", f"rm {current}")
    target = Path(recorded)
    if hub_alive(target):
        return Row(True, label, f"{target} (hub answers)")
    return Row(False, label, f"{target} (no hub there)", f"rm {current}")


def _row_hub(home: Path) -> Row:
    if hub_alive(home):
        return Row(True, "hub", f"{home}/hub.sock answers")
    return Row(False, "hub", f"no hub at {home}/hub.sock", f"moot serve --home {home}")


def _ctl_alive(path: Path) -> bool:
    """A bare connect: `CtlServer` answers nothing until a frame arrives, so
    this proves the socket has a live owner without sending one."""
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.settimeout(_PROBE_TIMEOUT)
        sock.connect(str(path))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _row_ctl(home: Path) -> Row:
    directory = home / "ctl"
    socks = sorted(directory.glob("*.sock")) if directory.is_dir() else []
    if not socks:
        return Row(True, "ctl sockets", f"none under {directory}")
    dead = [p for p in socks if not _ctl_alive(p)]
    detail = ", ".join(f"{p.stem} ({'dead' if p in dead else 'live'})" for p in socks)
    if dead:
        return Row(
            False,
            "ctl sockets",
            detail,
            f"rm {' '.join(str(p) for p in dead)} — the stream that owned it is gone",
        )
    return Row(True, "ctl sockets", detail)


def _ps_lines() -> list[str]:
    done = subprocess.run(
        list(_PS), capture_output=True, text=True, timeout=_PS_TIMEOUT
    )
    return done.stdout.splitlines()


def _subcommand(command: str) -> str | None:
    """`serve` or `stream` on a command line that mentions moot — both the
    `…/bin/moot serve …` and the `python -c … main() serve …` shapes."""
    if "moot" not in command:
        return None
    tokens = command.split()
    for index, token in enumerate(tokens):
        if token in ("serve", "stream") and index > 0:
            return token
    return None


def _home_of(command: str) -> Path:
    tokens = command.split()
    for index, token in enumerate(tokens):
        if token == "--home" and index + 1 < len(tokens):
            return Path(tokens[index + 1]).expanduser()
        if token.startswith("--home="):
            return Path(token.split("=", 1)[1]).expanduser()
    # `ps` shows no environment, so a MOOT_HOME-only process reads as ~/.moot.
    return Path.home() / ".moot"


def _row_orphans(home: Path) -> Row:
    label = "orphan processes"
    try:
        lines = _ps_lines()
    except (OSError, subprocess.SubprocessError) as exc:
        return Row(False, label, f"ps failed: {exc}")
    mine = os.path.realpath(home)
    found: list[tuple[str, str]] = []
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid, command = parts
        subcommand = _subcommand(command)
        if subcommand is None or pid == str(os.getpid()):
            continue
        proc_home = _home_of(command)
        if os.path.realpath(proc_home) == mine:
            continue
        found.append((pid, f"pid {pid} {subcommand} on {proc_home}"))
    if not found:
        return Row(True, label, f"none besides {home}")
    return Row(
        False,
        label,
        "; ".join(detail for _, detail in found),
        f"kill {' '.join(pid for pid, _ in found)}"
        " — a process started with MOOT_HOME and no --home reads as ~/.moot",
    )


def _print_row(row: Row) -> None:
    print(f"{'pass' if row.ok else 'FAIL'}  {row.label:<18}{row.detail}")
    if not row.ok and row.fix:
        print(f"      → {row.fix}")


def _roster(home: Path) -> tuple[Row, list[str]]:
    """Join as an observer, read one roster, leave. Every failure becomes the
    row: doctor never raises (see the module docstring)."""
    name = f"doctor-{os.getpid()}"
    lines: list[str] = []
    # `connect` builds its own socket, so the process-wide default is the only
    # timeout lever — a hub that accepts but never answers would park here.
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_ROSTER_TIMEOUT)
    try:
        conn, _welcome = connect(home, name, "observer", "", [])
        try:
            conn.send({"t": "roster"})
            for frame in conn.frames():
                if frame.get("t") == "ping":
                    conn.send({"t": "pong"})
                    continue
                if frame.get("t") == "roster":
                    lines = render(frame)
                    break
            conn.send({"t": "bye"})
        finally:
            conn.close()
    except (OSError, HubError) as exc:
        return Row(False, "roster", f"{name} could not read {home}: {exc}"), []
    finally:
        socket.setdefaulttimeout(old)
    return Row(True, "roster", f"joined as {name} · {len(lines)} peer(s)"), lines


def run_doctor(home: Path, roster: bool) -> int:
    repo = _repo_root()
    rows = [
        _row_path(),
        _row_editable(repo),
        _row_symlink(
            "claude skill",
            Path.home() / ".claude" / "skills" / "moot",
            repo / "skills" / "moot",
        ),
        _row_bun(repo),
        _row_symlink(
            "opencode plugin",
            Path.home() / ".config" / "opencode" / "plugins" / "moot.ts",
            repo / "opencode" / "moot.ts",
        ),
        _row_af_unix(home),
        _row_current(),
        _row_hub(home),
        _row_ctl(home),
        _row_orphans(home),
    ]
    for row in rows:
        _print_row(row)
    if roster:
        row, lines = _roster(home)
        _print_row(row)
        for line in lines:
            print(line)
    return 0
