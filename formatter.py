"""Message templating and deterministic length handling."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from .models import ParsedMessage
except ImportError:  # pragma: no cover - top-level module layout
    from models import ParsedMessage


_MISSING = object()


def _value(source: Any, key: str, default: Any = None) -> Any:
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


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def _coerce_limit(value: Any, default: int = 500) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _message_values(message: ParsedMessage | Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(message, ParsedMessage):
        values = message.to_dict()
        values["kind"] = message.kind.value
        values["timestamp"] = message.timestamp or ""
        return values
    if isinstance(message, Mapping):
        values = dict(message)
    else:
        values = {
            key: getattr(message, key)
            for key in ("sender", "message", "timestamp", "kind", "raw", "event_id", "event_name", "fingerprint")
            if hasattr(message, key)
        }
    if "message" not in values and "content" in values:
        values["message"] = values["content"]
    if hasattr(values.get("kind"), "value"):
        values["kind"] = values["kind"].value
    values.setdefault("sender", "")
    values.setdefault("message", "")
    values.setdefault("timestamp", "")
    values.setdefault("kind", "")
    return values


def _flatten_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """Expose useful raw fields without allowing raw data to replace core keys."""

    result = dict(values)
    raw = result.get("raw")
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            result.setdefault(str(key), value)
    return result


def _safe_format(template: str, values: Mapping[str, Any]) -> str:
    """Format known fields while preserving unknown placeholders literally."""

    class _Missing(dict[str, Any]):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(_Missing(values))
    except (ValueError, IndexError):
        # A malformed custom template should not drop a chat message.  The
        # fallback is still deterministic and preserves as much text as it can.
        return template


def _split_text(text: str, limit: int) -> tuple[str, ...]:
    if limit <= 0 or not text:
        return () if not text else (text,)
    if len(text) <= limit:
        return (text,)
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Prefer a boundary strictly inside the chunk.  Include the boundary
        # character in the preceding chunk so concatenating chunks reproduces
        # the original message exactly.
        newline = remaining.rfind("\n", 1, limit)
        space = remaining.rfind(" ", 1, limit)
        boundary = max(newline, space)
        cut = boundary + 1 if boundary > 0 else limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def split_message(text: str, max_length: int, *, split_long_messages: bool = True) -> tuple[str, ...]:
    """Split text into chunks no longer than ``max_length``.

    When splitting is disabled, the function returns one deterministic chunk;
    callers can then choose to reject or send it according to their adapter's
    policy.  The formatter itself never silently truncates message content.
    """

    text = str(text)
    limit = _coerce_limit(max_length)
    if not text:
        return ("",)
    if limit <= 0 or len(text) <= limit or not split_long_messages:
        return (text,)
    return _split_text(text, limit)


class MessageFormatter:
    """Render parsed messages using a configurable template."""

    def __init__(
        self,
        config: Any | None = None,
        *,
        template: str | None = None,
        max_length: int | None = None,
        split_long_messages: bool | None = None,
    ) -> None:
        # ``target`` usually contains the template while ``security`` owns the
        # length settings.  Accept either a complete config or a section.
        target = _value(config, "target", _MISSING)
        target = config if target is _MISSING or target is None else target
        security = _value(config, "security", _MISSING)
        security = None if security is _MISSING else security
        self.template = str(
            template if template is not None else _value(target, "message_template", "[Minecraft] <{sender}> {message}")
        )
        self.max_length = _coerce_limit(
            max_length if max_length is not None else _value(security, "max_message_length", _value(config, "max_message_length", 500)),
            500,
        )
        self.split_long_messages = _as_bool(
            split_long_messages
            if split_long_messages is not None
            else _value(security, "split_long_messages", _value(config, "split_long_messages", True)),
            True,
        )

    def render(self, message: ParsedMessage | Mapping[str, Any] | Any) -> str:
        values = _flatten_values(_message_values(message))
        return _safe_format(self.template, values)

    def format(self, message: ParsedMessage | Mapping[str, Any] | Any) -> str:
        return self.render(message)

    def split(self, text: str) -> tuple[str, ...]:
        return split_message(
            text,
            self.max_length,
            split_long_messages=self.split_long_messages,
        )

    def format_parts(self, message: ParsedMessage | Mapping[str, Any] | Any) -> tuple[str, ...]:
        return self.split(self.render(message))

    def format_message(self, message: ParsedMessage | Mapping[str, Any] | Any) -> tuple[str, ...]:
        return self.format_parts(message)


Formatter = MessageFormatter


def format_message(
    message: ParsedMessage | Mapping[str, Any] | Any,
    template: str = "[Minecraft] <{sender}> {message}",
    *,
    max_length: int = 500,
    split_long_messages: bool = True,
) -> tuple[str, ...]:
    """Functional convenience API used by small integrations and tests."""

    return MessageFormatter(
        template=template,
        max_length=max_length,
        split_long_messages=split_long_messages,
    ).format_parts(message)


__all__ = ["Formatter", "MessageFormatter", "format_message", "split_message"]
