"""Internal message representation and wire conversion."""

from dataclasses import dataclass
from typing import Any, Literal

State = Literal["idle", "busy", "blocked"]
Addressing = Literal["direct", "broadcast", "overheard"]


@dataclass
class Msg:
    id: int
    sender: str
    to: str  # original addressee ("*" for broadcast)
    kind: str
    text: str
    ts: float  # wall clock
    addressing: Addressing | None = None  # tagged per recipient by tag()
    seq: int | None = None  # sender-side sequence number
    private: bool = False  # delivered to the addressee and the observers only

    def tag(self, addressing: Addressing) -> "Msg":
        """Per-recipient copy with addressing annotation."""
        return Msg(
            id=self.id,
            sender=self.sender,
            to=self.to,
            kind=self.kind,
            text=self.text,
            ts=self.ts,
            addressing=addressing,
            seq=self.seq,
            private=self.private,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from": self.sender,
            "to": self.to,
            "addressing": self.addressing,
            "kind": self.kind,
            "text": self.text,
            "ts": self.ts,
            "seq": self.seq,
            **({"private": True} if self.private else {}),
        }


@dataclass
class Client:
    """A connected wire endpoint. Implemented by the asyncio server and by
    test fakes.

    Contract: send() must not block the caller and must not raise; a
    transport failure flips `connected` to False (the read loop then reaps
    the registration via Hub.disconnect)."""

    name: str = ""
    connected: bool = True

    async def send(self, frame: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        raise NotImplementedError
