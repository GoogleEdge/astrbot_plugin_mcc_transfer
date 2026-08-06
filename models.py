"""Domain models used by the MCC transfer plugin.

The models in this module deliberately do not import AstrBot.  This keeps the
filtering and delivery code usable from the command line and in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class MessageKind(StrEnum):
    CHAT = "chat"
    SYSTEM = "system"
    JOIN = "join"
    LEAVE = "leave"
    DEATH = "death"
    ADVANCEMENT = "advancement"
    ANNOUNCEMENT = "announcement"
    COMMAND = "command"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    sender: str
    message: str
    timestamp: str | None
    raw: Mapping[str, Any]
    kind: MessageKind = MessageKind.CHAT
    event_id: str | None = None
    event_name: str | None = None
    fingerprint: str = ""

    def __getitem__(self, key: str) -> Any:
        """Allow compatibility with the dictionary-shaped SPEC example."""
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "message": self.message,
            "timestamp": self.timestamp,
            "raw": dict(self.raw),
            "kind": self.kind.value,
            "event_id": self.event_id,
            "event_name": self.event_name,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class TargetRef:
    group_id: str
    platform_name: str = "qq_official"
    platform_instance: str = "default"
    message_type: str = "GroupMessage"
    umo_override: str | None = None

    @property
    def umo(self) -> str:
        if self.umo_override:
            return self.umo_override
        # AstrBot v4.26.8 MessageSession uses platform:type:session_id.
        # ``platform_instance`` is retained for legacy/custom templates but is
        # not part of the native UMO.
        return f"{self.platform_name}:{self.message_type}:{self.group_id}"


@dataclass(frozen=True, slots=True)
class FilterDecision:
    accepted: bool
    reason: str = "accepted"
    message: ParsedMessage | None = None


@dataclass(frozen=True, slots=True)
class DeliveryItem:
    fingerprint: str
    payloads: tuple[str, ...]
    sender: str
    original_message: str
    kind: str
    created_at: str
    target_umo: str
    attempt: int = 0
    event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "payloads": list(self.payloads),
            "sender": self.sender,
            "original_message": self.original_message,
            "kind": self.kind,
            "created_at": self.created_at,
            "target_umo": self.target_umo,
            "attempt": self.attempt,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DeliveryItem":
        return cls(
            fingerprint=str(value.get("fingerprint", "")),
            payloads=tuple(str(item) for item in value.get("payloads", [])),
            sender=str(value.get("sender", "")),
            original_message=str(value.get("original_message", "")),
            kind=str(value.get("kind", MessageKind.UNKNOWN.value)),
            created_at=str(value.get("created_at", "")),
            target_umo=str(value.get("target_umo", "")),
            attempt=int(value.get("attempt", 0)),
            event_id=(str(value["event_id"]) if value.get("event_id") is not None else None),
        )


@dataclass(slots=True)
class RetryRecord:
    record_id: str
    item: DeliveryItem
    attempts: int
    next_attempt_at: str
    last_error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "item": self.item.to_dict(),
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RetryRecord":
        return cls(
            record_id=str(value.get("record_id", "")),
            item=DeliveryItem.from_dict(value.get("item", {})),
            attempts=int(value.get("attempts", 0)),
            next_attempt_at=str(value.get("next_attempt_at", "")),
            last_error=str(value.get("last_error", "")),
            created_at=str(value.get("created_at", "")),
            expires_at=(str(value["expires_at"]) if value.get("expires_at") is not None else None),
        )


@dataclass(slots=True)
class RuntimeStatus:
    running: bool = False
    connected: bool = False
    received: int = 0
    forwarded: int = 0
    filtered: int = 0
    failed: int = 0
    retried: int = 0
    last_error: str | None = None
    last_message_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "connected": self.connected,
            "received": self.received,
            "forwarded": self.forwarded,
            "filtered": self.filtered,
            "failed": self.failed,
            "retried": self.retried,
            "last_error": self.last_error,
            "last_message_at": self.last_message_at,
        }
