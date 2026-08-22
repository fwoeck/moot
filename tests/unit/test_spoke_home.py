"""Home resolution and the ~/.moot/current rendezvous file, hub side included."""

import asyncio
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from moot.core.config import Config
from moot.core.server import serve
from moot.spoke.home import (
    clear_current,
    current_file,
    hub_alive,
    resolve_home,
    write_current,
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Point Path.home() at a scratch directory: the real ~/.moot/current may
    belong to a live hub."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("HOME", str(home))  # Path.expanduser() reads $HOME
    monkeypatch.delenv("MOOT_HOME", raising=False)
    return home


def _live_home(tmp_path: Path, name: str = "session") -> Path:
    home = tmp_path / name
    home.mkdir()
    (home / "hub.sock").touch()
    return home


def test_explicit_wins(fake_home: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOOT_HOME", str(tmp_path / "env"))
    write_current(_live_home(tmp_path))
    assert resolve_home(str(tmp_path / "explicit")) == tmp_path / "explicit"


def test_explicit_expands_tilde(fake_home: Path):
    assert resolve_home("~/somewhere") == fake_home / "somewhere"


def test_env_beats_current(fake_home: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOOT_HOME", "~/env-home")
    write_current(_live_home(tmp_path))
    assert resolve_home(None) == fake_home / "env-home"


def test_current_file_used_when_its_socket_exists(fake_home: Path, tmp_path: Path):
    live = _live_home(tmp_path)
    write_current(live)
    assert resolve_home(None) == live.resolve()


def test_stale_current_file_falls_back_to_default(fake_home: Path, tmp_path: Path):
    dead = tmp_path / "dead"
    dead.mkdir()
    write_current(dead)  # no hub.sock inside
    assert resolve_home(None) == fake_home / ".moot"


def test_empty_current_file_falls_back_to_default(fake_home: Path):
    current = current_file()
    current.parent.mkdir(mode=0o700)
    current.write_text("\n")
    assert resolve_home(None) == fake_home / ".moot"


def test_default_home(fake_home: Path):
    assert resolve_home(None) == fake_home / ".moot"


def test_write_current_creates_the_directory_0700(fake_home: Path, tmp_path: Path):
    live = _live_home(tmp_path)
    write_current(live)
    assert stat.S_IMODE(current_file().parent.stat().st_mode) == 0o700
    assert current_file().read_text() == f"{live.resolve()}\n"


def test_write_current_keeps_an_existing_directory(fake_home: Path, tmp_path: Path):
    (fake_home / ".moot").mkdir(mode=0o755)
    write_current(_live_home(tmp_path))
    assert stat.S_IMODE(current_file().parent.stat().st_mode) == 0o755


def test_clear_current_removes_only_its_own_entry(fake_home: Path, tmp_path: Path):
    mine = _live_home(tmp_path, "mine")
    theirs = _live_home(tmp_path, "theirs")
    write_current(theirs)
    clear_current(mine)
    assert current_file().exists()
    clear_current(theirs)
    assert not current_file().exists()


def test_clear_current_without_a_file_is_a_no_op(fake_home: Path, tmp_path: Path):
    clear_current(_live_home(tmp_path))
    assert not current_file().exists()


async def test_hub_publishes_and_removes_the_rendezvous_file(fake_home: Path):
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        config = Config(home=Path(d))
        config.notifications = False
        task = asyncio.create_task(serve(config))
        try:
            for _ in range(300):
                if current_file().exists():
                    break
                await asyncio.sleep(0.01)
            assert current_file().read_text() == f"{config.home.resolve()}\n"
            assert resolve_home(None) == config.home.resolve()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not current_file().exists()


def test_hub_alive_true_for_a_listener_false_otherwise():
    """A bare connect, no hello: the probe must not register on the floor."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="moot-") as d:
        home = Path(d)
        assert hub_alive(home, timeout=0.2) is False  # nothing there at all
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(home / "hub.sock"))
            sock.listen(1)
            assert hub_alive(home, timeout=0.2) is True
        finally:
            sock.close()
        assert (home / "hub.sock").exists()  # the file outlives the listener
        assert hub_alive(home, timeout=0.2) is False
