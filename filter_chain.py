"""Deterministic filtering for parsed Minecraft messages.

The filter chain intentionally has no AstrBot dependency.  It accepts either the
``ParsedMessage`` dataclass from :mod:`models` or a dictionary with the fields
from the SPEC.  The asynchronous ``check``/``apply`` methods are the preferred
API because the optional deduplication store may persist state; ``evaluate`` is
a synchronous convenience for callers that do not use persistence.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

try:  # The runtime is also usable as a collection of top-level modules.
    from .models import FilterDecision, MessageKind, ParsedMessage
except ImportError:  # pragma: no cover - exercised by the standalone layout
    from models import FilterDecision, MessageKind, ParsedMessage


# Keep this tuple public.  It is useful to tests and to status/debug tooling,
# and prevents an accidental reordering when a new rule is added.
FILTER_ORDER: tuple[str, ...] = (
    "bot_messages",
    "system_messages",
    "join_messages",
    "leave_messages",
    "death_messages",
    "advancement_messages",
    "server_announcements",
    "command_messages",
    "player_blacklist",
    "player_whitelist",
    "keyword_filter",
    "regex_filter",
    "empty_messages",
    "deduplication",
)

# A few spellings occur in MCC versions and in hand-written test fixtures.
_KIND_ALIASES: dict[str, MessageKind] = {
    "chat": MessageKind.CHAT,
    "player_message": MessageKind.CHAT,
    "player_chat": MessageKind.CHAT,
    "message": MessageKind.CHAT,
    "system": MessageKind.SYSTEM,
    "system_message": MessageKind.SYSTEM,
    "join": MessageKind.JOIN,
    "joined": MessageKind.JOIN,
    "player_join": MessageKind.JOIN,
    "leave": MessageKind.LEAVE,
    "left": MessageKind.LEAVE,
    "player_leave": MessageKind.LEAVE,
    "death": MessageKind.DEATH,
    "player_death": MessageKind.DEATH,
    "advancement": MessageKind.ADVANCEMENT,
    "progress": MessageKind.ADVANCEMENT,
    "achievement": MessageKind.ADVANCEMENT,
    "announcement": MessageKind.ANNOUNCEMENT,
    "server_announcement": MessageKind.ANNOUNCEMENT,
    "broadcast": MessageKind.ANNOUNCEMENT,
    "command": MessageKind.COMMAND,
    "unknown": MessageKind.UNKNOWN,
}


_MISSING = object()


def _value(source: Any, key: str, default: Any = None) -> Any:
    """Read a setting from mappings, dataclasses, or INI-like objects."""

    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    value = getattr(source, key, _MISSING)
    if value is not _MISSING:
        return value
    getter = getattr(source, "get", None)
    if getter is not None:
        try:
            return getter(key, default)
        except (AttributeError, KeyError, TypeError):
            pass
    return default


def _section(source: Any, key: str) -> Any:
    value = _value(source, key, _MISSING)
    return None if value is _MISSING else value


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "enable", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "n", "disable", "disabled", ""}:
        return False
    return default


def _items(value: Any) -> tuple[str, ...]:
    """Normalize comma/newline separated values without splitting spaces."""

    if value is None:
        return ()
    if isinstance(value, str):
        values: Sequence[Any] = re.split(r"[,\n\r]+", value)
    elif isinstance(value, Mapping):
        values = tuple(value.keys())
    else:
        try:
            values = tuple(value)
        except TypeError:
            values = (value,)
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _normalise_kind(value: Any) -> MessageKind:
    if isinstance(value, MessageKind):
        return value
    text = str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    return _KIND_ALIASES.get(text, MessageKind.UNKNOWN)


def fingerprint_for(message: ParsedMessage | Mapping[str, Any]) -> str:
    """Return the stable fingerprint used by the deduplication rule.

    An explicit event id is preferred because two equal chat messages can be
    distinct events.  Without one, sender, kind, and content form a stable
    short-lived content fingerprint; the store's TTL controls how long it is
    considered a duplicate.
    """

    if isinstance(message, ParsedMessage):
        if message.fingerprint:
            return str(message.fingerprint)
        sender = message.sender
        content = message.message
        kind = message.kind.value
        event_id = message.event_id
    else:
        explicit = message.get("fingerprint")
        if explicit:
            return str(explicit)
        sender = str(message.get("sender", ""))
        content = str(message.get("message", ""))
        kind = _normalise_kind(message.get("kind", MessageKind.UNKNOWN)).value
        event_id = message.get("event_id")
    if event_id is not None and str(event_id).strip():
        identity = f"event\0{event_id}"
    else:
        identity = f"content\0{sender}\0{kind}\0{content}"
    return hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()


def coerce_message(
    message: ParsedMessage | Mapping[str, Any],
    *,
    sender: str | None = None,
    content: str | None = None,
    raw: Mapping[str, Any] | None = None,
) -> ParsedMessage:
    """Coerce a parser result or SPEC-shaped dictionary to ``ParsedMessage``."""

    if isinstance(message, ParsedMessage):
        if message.fingerprint:
            return message
        return replace(message, fingerprint=fingerprint_for(message))
    source = message
    actual_sender = str(sender if sender is not None else source.get("sender", ""))
    actual_content = str(content if content is not None else source.get("message", source.get("content", "")))
    actual_kind = _normalise_kind(source.get("kind", source.get("type", MessageKind.UNKNOWN)))
    actual_raw = raw if raw is not None else source.get("raw", source)
    if not isinstance(actual_raw, Mapping):
        actual_raw = {"value": actual_raw}
    result = ParsedMessage(
        sender=actual_sender,
        message=actual_content,
        timestamp=(str(source["timestamp"]) if source.get("timestamp") is not None else None),
        raw=actual_raw,
        kind=actual_kind,
        event_id=(str(source["event_id"]) if source.get("event_id") is not None else None),
        event_name=(str(source["event_name"]) if source.get("event_name") is not None else None),
        fingerprint=str(source.get("fingerprint", "")),
    )
    return result if result.fingerprint else replace(result, fingerprint=fingerprint_for(result))


class FilterChain:
    """Apply all configured filters in the fixed order from the SPEC.

    A filter that is disabled is skipped.  The global ``enabled`` setting means
    "disable filtering", so a disabled chain accepts the message unchanged.
    Blacklist evaluation is deliberately before whitelist evaluation and is
    never bypassed by a whitelist match.
    """

    order = FILTER_ORDER

    def __init__(
        self,
        config: Any | None = None,
        *,
        dedup: Any | None = None,
        dedup_store: Any | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # A complete AppConfig, its ``filter`` section, or a plain mapping are
        # all accepted.  If a complete config is supplied, prefer its section.
        section = _section(config, "filter")
        self.config = section if section is not None else (config or {})
        self._root_config = config
        self.dedup = dedup if dedup is not None else dedup_store
        self._clock = clock or time.time

        self.enabled = _as_bool(_value(self.config, "enabled", True), True)
        self.ignore_bot_messages = _as_bool(_value(self.config, "ignore_bot_messages", True), True)
        self.bot_names = _items(_value(self.config, "bot_names", None)) or _items(
            _value(self.config, "bot_name", None)
        )
        self.ignore_system_messages = _as_bool(_value(self.config, "ignore_system_messages", True), True)
        self.ignore_join_messages = _as_bool(_value(self.config, "ignore_join_messages", True), True)
        self.ignore_leave_messages = _as_bool(_value(self.config, "ignore_leave_messages", True), True)
        self.ignore_death_messages = _as_bool(_value(self.config, "ignore_death_messages", True), True)
        self.ignore_advancement_messages = _as_bool(
            _value(self.config, "ignore_advancement_messages", True), True
        )
        self.ignore_server_announcements = _as_bool(
            _value(self.config, "ignore_server_announcements", True), True
        )
        self.ignore_command_messages = _as_bool(_value(self.config, "ignore_command_messages", True), True)
        self.command_prefixes = _items(_value(self.config, "command_prefixes", ("/", "!")))

        self.enable_player_blacklist = _as_bool(
            _value(self.config, "enable_player_blacklist", False), False
        )
        self.blacklist_players = frozenset(_items(_value(self.config, "blacklist_players", ())))
        self.enable_player_whitelist = _as_bool(
            _value(self.config, "enable_player_whitelist", False), False
        )
        self.whitelist_players = frozenset(_items(_value(self.config, "whitelist_players", ())))

        self.enable_keyword_filter = _as_bool(_value(self.config, "enable_keyword_filter", False), False)
        self.blocked_keywords = _items(_value(self.config, "blocked_keywords", ()))
        self.keyword_case_sensitive = _as_bool(
            _value(self.config, "keyword_case_sensitive", False), False
        )
        self.keyword_whole_word = _as_bool(_value(self.config, "keyword_whole_word", False), False)

        self.enable_regex_filter = _as_bool(_value(self.config, "enable_regex_filter", False), False)
        self.blocked_regex = _items(_value(self.config, "blocked_regex", ()))
        self.regex_errors: tuple[str, ...] = ()
        compiled: list[re.Pattern[str]] = []
        errors: list[str] = []
        for expression in self.blocked_regex:
            try:
                compiled.append(re.compile(expression))
            except re.error as exc:
                # A malformed optional rule must not crash the message loop.
                # Keep the error visible for diagnostics, but ignore that rule.
                errors.append(f"{expression}: {exc}")
        self._regexes = tuple(compiled)
        self.regex_errors = tuple(errors)

        self.ignore_empty_messages = _as_bool(_value(self.config, "ignore_empty_messages", True), True)
        dedup_section = _section(config, "dedup")
        configured_dedup = _value(self.config, "dedup_enabled", _MISSING)
        if configured_dedup is _MISSING:
            configured_dedup = _value(dedup_section, "enabled", True)
        self.dedup_enabled = _as_bool(configured_dedup, True) and self.dedup is not None

    def _decision(self, accepted: bool, reason: str, message: ParsedMessage) -> FilterDecision:
        return FilterDecision(accepted=accepted, reason=reason, message=message)

    def _static_decision(self, message: ParsedMessage | Mapping[str, Any]) -> FilterDecision:
        parsed = coerce_message(message)
        if not self.enabled:
            return self._decision(True, "filters_disabled", parsed)

        kind = parsed.kind
        raw = parsed.raw
        sender = parsed.sender
        text = parsed.message

        # 1. Bot messages.
        raw_is_bot = _as_bool(raw.get("is_bot", raw.get("bot", False)), False)
        if self.ignore_bot_messages and (
            raw_is_bot or (self.bot_names and sender in self.bot_names)
        ):
            return self._decision(False, "bot_message", parsed)
        # 2-7. Message kinds.
        checks: tuple[tuple[bool, MessageKind, str], ...] = (
            (self.ignore_system_messages, MessageKind.SYSTEM, "system_message"),
            (self.ignore_join_messages, MessageKind.JOIN, "join_message"),
            (self.ignore_leave_messages, MessageKind.LEAVE, "leave_message"),
            (self.ignore_death_messages, MessageKind.DEATH, "death_message"),
            (self.ignore_advancement_messages, MessageKind.ADVANCEMENT, "advancement_message"),
            (self.ignore_server_announcements, MessageKind.ANNOUNCEMENT, "server_announcement"),
        )
        for active, expected_kind, reason in checks:
            if active and kind is expected_kind:
                return self._decision(False, reason, parsed)

        # 8. Command messages are identified both by parser kind and prefix.
        if self.ignore_command_messages and (
            kind is MessageKind.COMMAND
            or any(text.startswith(prefix) for prefix in self.command_prefixes)
        ):
            return self._decision(False, "command_message", parsed)

        # 9. Blacklist intentionally precedes the whitelist.
        if self.enable_player_blacklist and sender in self.blacklist_players:
            return self._decision(False, "blacklisted_player", parsed)
        # 10. A whitelist is an allow-list, not an override for blacklist.
        if self.enable_player_whitelist and sender not in self.whitelist_players:
            return self._decision(False, "not_whitelisted_player", parsed)

        # 11. Keyword filtering.
        if self.enable_keyword_filter and self.blocked_keywords:
            haystack = text if self.keyword_case_sensitive else text.casefold()
            for keyword in self.blocked_keywords:
                needle = keyword if self.keyword_case_sensitive else keyword.casefold()
                if self.keyword_whole_word:
                    # ``\w`` handles ASCII and Unicode word characters while
                    # lookarounds avoid altering the original message.
                    pattern = rf"(?<!\w){re.escape(needle)}(?!\w)"
                    if re.search(pattern, haystack):
                        return self._decision(False, "blocked_keyword", parsed)
                elif needle in haystack:
                    return self._decision(False, "blocked_keyword", parsed)

        # 12. Regex filtering.
        if self.enable_regex_filter:
            for expression in self._regexes:
                if expression.search(text):
                    return self._decision(False, "blocked_regex", parsed)

        # 13. Empty messages are checked after content rules by design.
        if self.ignore_empty_messages and not text.strip():
            return self._decision(False, "empty_message", parsed)

        # Deduplication is the final rule.  The async path performs the same
        # check atomically with the store's update.
        return self._decision(True, "accepted", parsed)

    def _dedup_sync(self, decision: FilterDecision) -> FilterDecision:
        if not decision.accepted or not self.dedup_enabled or decision.message is None:
            return decision
        fingerprint = decision.message.fingerprint or fingerprint_for(decision.message)
        message = decision.message
        if hasattr(self.dedup, "check_and_add_sync"):
            duplicate = bool(self.dedup.check_and_add_sync(fingerprint, now=self._clock()))
        elif hasattr(self.dedup, "check_and_record_sync"):
            duplicate = bool(self.dedup.check_and_record_sync(fingerprint, now=self._clock()))
        else:
            # A small compatibility path for user-provided stores.
            duplicate = bool(self.dedup.contains(fingerprint, now=self._clock())) if hasattr(self.dedup, "contains") else False
            if not duplicate and hasattr(self.dedup, "add_sync"):
                self.dedup.add_sync(fingerprint, now=self._clock())
        if duplicate:
            return self._decision(False, "duplicate_message", message)
        return decision

    def evaluate(self, message: ParsedMessage | Mapping[str, Any]) -> FilterDecision:
        """Synchronously evaluate a message, including an in-memory dedup store."""

        return self._dedup_sync(self._static_decision(message))

    async def check(self, message: ParsedMessage | Mapping[str, Any]) -> FilterDecision:
        """Asynchronously evaluate and atomically record accepted fingerprints."""

        decision = self._static_decision(message)
        if not decision.accepted or not self.dedup_enabled or decision.message is None:
            return decision
        fingerprint = decision.message.fingerprint or fingerprint_for(decision.message)
        duplicate: bool
        if hasattr(self.dedup, "check_and_add"):
            duplicate = bool(await self.dedup.check_and_add(fingerprint, now=self._clock()))
        elif hasattr(self.dedup, "check_and_record"):
            duplicate = bool(await self.dedup.check_and_record(fingerprint, now=self._clock()))
        else:
            # User stores that only expose synchronous operations remain usable.
            duplicate = self._dedup_sync(decision).accepted is False
        if duplicate:
            return self._decision(False, "duplicate_message", decision.message)
        return decision

    async def apply(self, message: ParsedMessage | Mapping[str, Any]) -> FilterDecision:
        """Alias for :meth:`check`, matching common filter-chain terminology."""

        return await self.check(message)

    async def filter_message(self, message: ParsedMessage | Mapping[str, Any]) -> FilterDecision:
        return await self.check(message)

    def filter(self, message: ParsedMessage | Mapping[str, Any]) -> FilterDecision:
        """Synchronous alias for callers that do not need async persistence."""

        return self.evaluate(message)


__all__ = [
    "FILTER_ORDER",
    "FilterChain",
    "coerce_message",
    "fingerprint_for",
]
