"""Shared test scaffolding: fake clock, fake clients, hub factory."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from moot.core.clock import FakeClock
from moot.core.config import Config
from moot.core.hub import Hub
from moot.core.notify import CollectingNotifier
from moot.core.transcript import Transcript
from moot.core.types import Client


class FakeClient(Client):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        self.closed = True
        self.connected = False

    def frames(self, t: str) -> list[dict[str, Any]]:
        return [f for f in self.sent if f.get("t") == t]

    def last(self, t: str) -> dict[str, Any] | None:
        frames = self.frames(t)
        return frames[-1] if frames else None

    def clear(self) -> None:
        self.sent.clear()


class DyingClient(FakeClient):
    """Models the new Client contract for a peer whose transport is gone:
    send() never raises; from the first frame of type `die_on` on it flips
    connected=False and drops frames. `die_on="welcome"` = dead from the
    moment it joined (routing sees connected=False); `die_on="deliver"` =
    dies mid-delivery (the frame in flight is lost, as on a real socket)."""

    def __init__(self, die_on: str = "deliver") -> None:
        super().__init__()
        self.die_on = die_on
        self.dropped: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        if frame.get("t") == self.die_on or not self.connected:
            self.connected = False
            self.dropped.append(frame)
            return
        await super().send(frame)


class YieldingClient(FakeClient):
    """send() yields to the event loop once, so two hub.handle() calls can
    interleave — used to prove the lock/snapshot discipline."""

    async def send(self, frame: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        await super().send(frame)


class Harness:
    def __init__(self, tmp_path: Path, config: Config | None = None) -> None:
        self.config = config or Config(home=tmp_path)
        self.config.notifications = True
        self.clock = FakeClock()
        self.notifier = CollectingNotifier()
        self.transcript = Transcript(self.config.transcript_dir, self.clock)
        self.hub = Hub(self.config, self.clock, self.transcript, self.notifier)
        self.clients: dict[str, FakeClient] = {}
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def join_with(
        self,
        client: FakeClient,
        name: str,
        kind: str = "opencode",
        caps: list[str] | None = None,
        role: str = "",
        room: str | None = None,
    ) -> FakeClient:
        """join() with a caller-supplied client double."""
        frame: dict[str, Any] = {
            "t": "hello",
            "proto": 1,
            "name": name,
            "kind": kind,
            "caps": ["idle-events"] if caps is None else caps,
            "role": role,
        }
        if room is not None:
            frame["room"] = room
        await self.hub.handle(client, frame)
        self.clients[name] = client
        return client

    async def join(
        self,
        name: str,
        kind: str = "opencode",
        caps: list[str] | None = None,
        role: str = "",
        room: str | None = None,
    ) -> FakeClient:
        return await self.join_with(FakeClient(), name, kind, caps, role, room)

    def ok_or_err(self, name: str) -> dict[str, Any] | None:
        """The last ok/err frame a client received (for seq/echo assertions)."""
        frames = [f for f in self.clients[name].sent if f.get("t") in ("ok", "err")]
        return frames[-1] if frames else None

    async def say(
        self,
        sender: str,
        to: str,
        text: str,
        kind: str = "note",
        private: bool = False,
    ) -> None:
        await self.hub.handle(
            self.clients[sender],
            {
                "t": "say",
                "to": to,
                "kind": kind,
                "text": text,
                "seq": self.next_seq(),
                **({"private": True} if private else {}),
            },
        )

    async def state(self, name: str, state: str, detail: str | None = None) -> None:
        await self.hub.handle(
            self.clients[name], {"t": "state", "state": state, "detail": detail}
        )

    async def cmd(
        self, sender: str, cmd: str, args: dict[str, Any] | None = None
    ) -> None:
        frame: dict[str, Any] = {"t": "cmd", "cmd": cmd, "seq": self.next_seq()}
        if args:
            frame["args"] = args
        await self.hub.handle(self.clients[sender], frame)

    async def roster(self, name: str, seq: int | None = None) -> None:
        frame: dict[str, Any] = {"t": "roster"}
        if seq is not None:
            frame["seq"] = seq
        await self.hub.handle(self.clients[name], frame)

    def delivered(self, name: str) -> list[dict[str, Any]]:
        """All msgs across all deliver frames a client received, flattened."""
        out: list[dict[str, Any]] = []
        for frame in self.clients[name].frames("deliver"):
            out.extend(frame["msgs"])
        return out


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
