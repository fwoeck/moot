"""Finding the state directory: `--home` → `$MOOT_HOME` → the rendezvous
file `~/.moot/current` → `~/.moot`.

The hub writes the rendezvous file at startup and removes it on shutdown, so
a spoke started from any directory finds the running session without a pasted
path.
"""

import os
import socket
from pathlib import Path


def current_file() -> Path:
    """`~/.moot/current`. Resolved per call so a changed $HOME is honoured."""
    return Path.home() / ".moot" / "current"


def resolve_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("MOOT_HOME")
    if env:
        return Path(env).expanduser()
    current = current_file()
    if current.exists():
        recorded = current.read_text(encoding="utf-8").strip()
        # A stale file (hub gone, socket removed) must not shadow ~/.moot.
        if recorded and (Path(recorded) / "hub.sock").exists():
            return Path(recorded)
    return Path.home() / ".moot"


def write_current(home: Path) -> None:
    current = current_file()
    if not current.parent.exists():
        current.parent.mkdir(parents=True, mode=0o700)
    current.write_text(f"{home.resolve()}\n", encoding="utf-8")


def clear_current(home: Path) -> None:
    """Remove the rendezvous file, but only while it still points here."""
    current = current_file()
    if not current.exists():
        return
    if current.read_text(encoding="utf-8").strip() == str(home.resolve()):
        current.unlink()


def hub_alive(home: Path, timeout: float = 0.5) -> bool:
    """A bare connect to <home>/hub.sock — no hello, no registration, no effect
    on the floor (an unregistered client's disconnect is a no-op in the hub)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(home / "hub.sock"))
    except OSError:
        return False
    finally:
        sock.close()
    return True
