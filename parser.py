"""Deterministic parsing of MCC MCP event frames.

The parser deliberately does not perform network I/O or call an LLM.  It
accepts the default MCC event shape from ``SPEC.md`` and a small configurable
set of field paths for MCC wrappers/versions which nest or rename fields.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

try:  # The repository keeps the domain models at its root.
    from models import MessageKind, ParsedMessage
except ImportError:  # pragma: no cover - useful when vendored as a package
    from .models import MessageKind, ParsedMessage  # type: ignore[no-redef]

from protocol import PathSpec, get_path

_MISSING = object()


_DEFAULT_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "event_id",
    "event_name",
    "kind",
    "sender",
    "message",
    "timestamp",
)


_DEFAULT_PATHS: dict[str, tuple[str, ...]] = {
    "event_type": ("type",),
    "event_name": ("event", "event_name", "name"),
    "sender": ("player", "sender", "username", "user", "player_name"),
    "message": ("message", "text", "content"),
    "timestamp": ("timestamp", "time", "created_at", "occurred_at"),
    "event_id": ("event_id", "id"),
    "kind": ("kind", "message_kind", "message_type"),
}

_EVENT_CONTAINER_PATHS: tuple[str, ...] = (
    "payload",
    "data",
    "event_data",
    "params",
    "result",
)


class ParseError(ValueError):
    """Raised when a frame is an event but cannot produce a message."""


@dataclass(frozen=True, slots=True)
class ParserConfig:
    """Field and inference settings for :class:`MessageParser`.

    ``field_paths`` values may be dotted paths (``payload.player``), JSON
    pointer-like paths (``/payload/player``), or sequences of fallback paths.
    The defaults try the top-level MCC names first and then common nested
    containers such as ``payload`` and ``data``.
    """

    event_name: str = "PlayerMessage"
    event_type_path: PathSpec | None = "type"
    event_name_path: PathSpec | None = "event"
    field_paths: Mapping[str, PathSpec] = field(default_factory=dict)
    accepted_event_names: tuple[str, ...] = ()
    ignore_unknown_events: bool = True
    allow_missing_sender: bool = False
    allow_missing_message: bool = False
    require_event_marker: bool = False
    fingerprint_fields: tuple[str, ...] = (
        "event_id",
        "event_name",
        "kind",
        "sender",
        "message",
        "timestamp",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ParserConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        data = dict(value)
        paths = data.get("field_paths", data.get("fields", {}))
        if not isinstance(paths, Mapping):
            raise TypeError("parser field_paths must be a mapping")
        accepted = data.get("accepted_event_names", data.get("events", ()))
        if isinstance(accepted, str):
            accepted = (accepted,)
        else:
            accepted = tuple(str(item) for item in accepted)
        fingerprint_fields = data.get("fingerprint_fields", _DEFAULT_FINGERPRINT_FIELDS)
        if isinstance(fingerprint_fields, str):
            fingerprint_fields = tuple(
                item.strip() for item in fingerprint_fields.split(",") if item.strip()
            )
        else:
            fingerprint_fields = tuple(str(item) for item in fingerprint_fields)
        return cls(
            event_name=str(data.get("event_name", "PlayerMessage")),
            event_type_path=data.get("event_type_path", "type"),
            event_name_path=data.get("event_name_path", "event"),
            field_paths=dict(paths),
            accepted_event_names=accepted,
            ignore_unknown_events=bool(data.get("ignore_unknown_events", True)),
            allow_missing_sender=bool(data.get("allow_missing_sender", False)),
            allow_missing_message=bool(data.get("allow_missing_message", False)),
            require_event_marker=bool(data.get("require_event_marker", False)),
            fingerprint_fields=fingerprint_fields,
        )

    def path_for(self, field_name: str) -> PathSpec | None:
        configured = self.field_paths.get(field_name)
        if configured is not None:
            return configured
        defaults = _DEFAULT_PATHS.get(field_name)
        if defaults is None:
            return None
        return defaults[0] if len(defaults) == 1 else defaults


# Event names have changed spelling in a few MCC wrappers.  Keep inference
# deterministic and conservative: unknown names are chat only when a message
# field is present, not based on arbitrary free-form text.
_KIND_ALIASES: dict[str, MessageKind] = {
    "chat": MessageKind.CHAT,
    "player_message": MessageKind.CHAT,
    "playermessage": MessageKind.CHAT,
    "message": MessageKind.CHAT,
    "system": MessageKind.SYSTEM,
    "system_message": MessageKind.SYSTEM,
    "systemmessage": MessageKind.SYSTEM,
    "join": MessageKind.JOIN,
    "joined": MessageKind.JOIN,
    "player_join": MessageKind.JOIN,
    "player_joined": MessageKind.JOIN,
    "playerjoin": MessageKind.JOIN,
    "leave": MessageKind.LEAVE,
    "left": MessageKind.LEAVE,
    "quit": MessageKind.LEAVE,
    "player_leave": MessageKind.LEAVE,
    "player_left": MessageKind.LEAVE,
    "playerleave": MessageKind.LEAVE,
    "death": MessageKind.DEATH,
    "player_death": MessageKind.DEATH,
    "playerdeath": MessageKind.DEATH,
    "advancement": MessageKind.ADVANCEMENT,
    "achievement": MessageKind.ADVANCEMENT,
    "advancement_made": MessageKind.ADVANCEMENT,
    "server_announcement": MessageKind.ANNOUNCEMENT,
    "announcement": MessageKind.ANNOUNCEMENT,
    "broadcast": MessageKind.ANNOUNCEMENT,
    "command": MessageKind.COMMAND,
    "command_message": MessageKind.COMMAND,
}

_KIND_PATTERN_ALIASES: tuple[tuple[re.Pattern[str], MessageKind], ...] = (
    (re.compile(r"(?:^|_)(?:player_)?join(?:ed)?(?:$|_)", re.I), MessageKind.JOIN),
    (re.compile(r"(?:^|_)(?:player_)?(?:leave|left|quit)(?:$|_)", re.I), MessageKind.LEAVE),
    (re.compile(r"(?:^|_)(?:player_)?death(?:$|_)", re.I), MessageKind.DEATH),
    (re.compile(r"(?:^|_)(?:achievement|advancement)(?:$|_)", re.I), MessageKind.ADVANCEMENT),
    (re.compile(r"(?:^|_)(?:announcement|broadcast)(?:$|_)", re.I), MessageKind.ANNOUNCEMENT),
    (re.compile(r"(?:^|_)command(?:$|_)", re.I), MessageKind.COMMAND),
    (re.compile(r"(?:^|_)system(?:$|_)", re.I), MessageKind.SYSTEM),
)


def _as_text(value: Any, *, field_name: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if value is None:
        return ""
    if isinstance(value, Mapping):
        # Structured chat content is common in wrappers.  Prefer familiar
        # text keys, then recursively collect text-like children.
        for key in ("text", "message", "content", "value"):
            if key in value:
                return _as_text(value[key], field_name=field_name)
        pieces: list[str] = []
        for child in value.values():
            if isinstance(child, (str, int, float, bool)):
                pieces.append(str(child))
        return " ".join(pieces)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "".join(_as_text(item, field_name=field_name) for item in value)
    return str(value)


def _as_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float)):
        return str(value)
    return _as_text(value, field_name="timestamp") or None


def _normalise_name(value: Any) -> str:
    return _as_text(value, field_name="event_name").strip()


def _normalise_kind(value: Any) -> MessageKind | None:
    if value is None:
        return None
    if isinstance(value, MessageKind):
        return value
    candidate = _as_text(value, field_name="kind").strip().casefold()
    if not candidate:
        return None
    try:
        return MessageKind(candidate)
    except ValueError:
        return _KIND_ALIASES.get(candidate)


def _kind_from_name(event_name: str, event_type: str = "") -> MessageKind:
    for candidate in (event_name, event_type):
        key = candidate.strip().casefold().replace("-", "_").replace(" ", "_")
        if key in _KIND_ALIASES:
            return _KIND_ALIASES[key]
        for pattern, kind in _KIND_PATTERN_ALIASES:
            if pattern.search(key):
                return kind
    return MessageKind.CHAT


def _path_value(raw: Mapping[str, Any], config: ParserConfig, field_name: str) -> Any:
    configured = config.field_paths.get(field_name)
    if configured is not None:
        return get_path(raw, configured, _MISSING)

    paths = _DEFAULT_PATHS.get(field_name, ())
    value = get_path(raw, paths, _MISSING)
    if value is not _MISSING:
        return value
    # When fields are nested below a payload/data object, search the same
    # aliases there.  This fallback only traverses explicitly known containers
    # and therefore does not accidentally parse arbitrary metadata.
    for container in _EVENT_CONTAINER_PATHS:
        nested = get_path(raw, container, _MISSING)
        if nested is _MISSING:
            continue
        value = get_path(nested, paths, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _event_marker(raw: Mapping[str, Any], config: ParserConfig) -> tuple[str, str, bool]:
    type_value = get_path(raw, config.event_type_path, _MISSING)
    event_value = get_path(raw, config.event_name_path, _MISSING)
    if event_value is _MISSING:
        event_value = _path_value(raw, config, "event_name")
    if type_value is _MISSING:
        type_value = _path_value(raw, config, "event_type")
    event_name = "" if event_value is _MISSING else _normalise_name(event_value)
    event_type = "" if type_value is _MISSING else _normalise_name(type_value)
    marker_present = event_value is not _MISSING or type_value is not _MISSING
    return event_name, event_type, marker_present


def _accepted_event_name(event_name: str, config: ParserConfig) -> bool:
    accepted = config.accepted_event_names or (config.event_name,)
    if not event_name:
        return not config.require_event_marker
    return any(event_name.casefold() == name.casefold() for name in accepted)


def fingerprint_for(
    *,
    sender: str,
    message: str,
    timestamp: str | None = None,
    kind: MessageKind | str = MessageKind.CHAT,
    event_id: str | None = None,
    event_name: str | None = None,
    fields: Sequence[str] | None = None,
    algorithm: str = "sha256",
) -> str:
    """Return a stable lower-case digest for message identity.

    The input is canonical JSON with sorted keys, fixed separators, and UTF-8
    encoding.  Consequently equivalent frames with different dictionary key
    order produce the same fingerprint.  ``event_id`` is included when
    available, which makes redelivery of one server event deduplicate without
    collapsing two independently-created identical chats.
    """

    resolved_kind = kind.value if isinstance(kind, MessageKind) else str(kind)
    values: dict[str, Any] = {
        "event_id": event_id,
        "event_name": event_name,
        "kind": resolved_kind,
        "sender": sender,
        "message": message,
        "timestamp": timestamp,
    }
    selected = tuple(fields) if fields is not None else tuple(values)
    canonical = {name: values.get(name) for name in selected}
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise ValueError(f"unsupported fingerprint algorithm: {algorithm}") from exc
    digest.update(encoded)
    return digest.hexdigest()


class MessageParser:
    """Parse raw MCP frames into :class:`models.ParsedMessage` values."""

    def __init__(
        self,
        config: ParserConfig | Mapping[str, Any] | None = None,
        *,
        field_paths: Mapping[str, PathSpec] | None = None,
        event_name: str | None = None,
        **config_overrides: Any,
    ) -> None:
        if config is None:
            config = {}
        if isinstance(config, ParserConfig):
            parsed_config = config
        else:
            values = dict(config)
            if field_paths is not None:
                values["field_paths"] = field_paths
            if event_name is not None:
                values["event_name"] = event_name
            values.update(config_overrides)
            parsed_config = ParserConfig.from_mapping(values)
        if field_paths is not None or event_name is not None or config_overrides:
            values = {
                "field_paths": field_paths or parsed_config.field_paths,
                "event_name": event_name or parsed_config.event_name,
                "event_type_path": parsed_config.event_type_path,
                "event_name_path": parsed_config.event_name_path,
                "accepted_event_names": parsed_config.accepted_event_names,
                "ignore_unknown_events": parsed_config.ignore_unknown_events,
                "allow_missing_sender": parsed_config.allow_missing_sender,
                "allow_missing_message": parsed_config.allow_missing_message,
                "require_event_marker": parsed_config.require_event_marker,
                "fingerprint_fields": parsed_config.fingerprint_fields,
            }
            values.update(config_overrides)
            parsed_config = ParserConfig.from_mapping(values)
        self.config = parsed_config

    def _parse(self, raw: Mapping[str, Any], *, strict: bool = False) -> ParsedMessage | None:
        if not isinstance(raw, Mapping):
            raise ParseError("MCP event must be a mapping")
        raw_copy = dict(raw)
        event_name, event_type, marker_present = _event_marker(raw_copy, self.config)
        if self.config.require_event_marker and not marker_present:
            if strict:
                raise ParseError("MCP frame has no event marker")
            return None
        if not _accepted_event_name(event_name, self.config):
            if self.config.ignore_unknown_events and not strict:
                return None
            raise ParseError(f"unexpected MCP event: {event_name or event_type or '<unknown>'}")

        sender_value = _path_value(raw_copy, self.config, "sender")
        message_value = _path_value(raw_copy, self.config, "message")
        missing_sender = sender_value is _MISSING or sender_value is None
        missing_message = message_value is _MISSING or message_value is None
        if (missing_sender and not self.config.allow_missing_sender) or (
            missing_message and not self.config.allow_missing_message
        ):
            if strict:
                missing = []
                if missing_sender:
                    missing.append("sender")
                if missing_message:
                    missing.append("message")
                raise ParseError("MCP event is missing " + ", ".join(missing))
            return None

        sender = "" if missing_sender else _as_text(sender_value, field_name="sender")
        message = "" if missing_message else _as_text(message_value, field_name="message")
        timestamp_value = _path_value(raw_copy, self.config, "timestamp")
        event_id_value = _path_value(raw_copy, self.config, "event_id")
        explicit_kind_value = _path_value(raw_copy, self.config, "kind")
        explicit_kind = None if explicit_kind_value is _MISSING else _normalise_kind(explicit_kind_value)
        kind = explicit_kind or _kind_from_name(event_name, event_type)
        timestamp = None if timestamp_value is _MISSING else _as_timestamp(timestamp_value)
        event_id = (
            None
            if event_id_value is _MISSING or event_id_value is None
            else _as_text(event_id_value, field_name="event_id")
        )
        fingerprint = fingerprint_for(
            sender=sender,
            message=message,
            timestamp=timestamp,
            kind=kind,
            event_id=event_id,
            event_name=event_name or event_type or None,
            fields=self.config.fingerprint_fields,
        )
        return ParsedMessage(
            sender=sender,
            message=message,
            timestamp=timestamp,
            raw=raw_copy,
            kind=kind,
            event_id=event_id,
            event_name=event_name or event_type or None,
            fingerprint=fingerprint,
        )

    def parse(self, raw: Mapping[str, Any], *, strict: bool = False) -> ParsedMessage | None:
        """Parse a frame, returning ``None`` for non-message frames by default."""

        return self._parse(raw, strict=strict)

    def parse_event(self, raw: Mapping[str, Any], *, strict: bool = False) -> ParsedMessage | None:
        return self._parse(raw, strict=strict)

    def __call__(self, raw: Mapping[str, Any], *, strict: bool = False) -> ParsedMessage | None:
        return self._parse(raw, strict=strict)


# Short class aliases make the public API pleasant without duplicating logic.
Parser = MessageParser
MCPParser = MessageParser


def parse_event(
    raw: Mapping[str, Any],
    config: ParserConfig | Mapping[str, Any] | None = None,
    *,
    strict: bool = False,
    **config_overrides: Any,
) -> ParsedMessage | None:
    """Parse one event using a new parser instance."""

    return MessageParser(config, **config_overrides).parse(raw, strict=strict)


def parse_message(
    raw: Mapping[str, Any],
    config: ParserConfig | Mapping[str, Any] | None = None,
    *,
    strict: bool = False,
    **config_overrides: Any,
) -> ParsedMessage | None:
    return parse_event(raw, config, strict=strict, **config_overrides)


def parse_mcp_message(
    raw: Mapping[str, Any],
    config: ParserConfig | Mapping[str, Any] | None = None,
    *,
    strict: bool = False,
    **config_overrides: Any,
) -> ParsedMessage | None:
    return parse_event(raw, config, strict=strict, **config_overrides)


__all__ = [
    "MCPParser",
    "MessageKind",
    "MessageParser",
    "ParseError",
    "ParsedMessage",
    "Parser",
    "ParserConfig",
    "fingerprint_for",
    "parse_event",
    "parse_message",
    "parse_mcp_message",
]
