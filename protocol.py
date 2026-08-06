"""Wire-level helpers for the MCC JSON-over-WebSocket protocol.

The MCC MCP server used by this plugin speaks a deliberately small JSON
protocol.  The default frames in this module are the frames documented in
``SPEC.md``; the configuration objects make the same client usable with MCC
versions which put fields below a payload object or use different names.

This module contains no WebSocket code.  It is intentionally small and
synchronous so it can also be used by test servers and command-line tools.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, TypeAlias

JSONValue: TypeAlias = Any
FrameTemplate: TypeAlias = Mapping[str, Any] | str
PathSpec: TypeAlias = str | Sequence[str]


# These are mappings rather than JSON strings so callers can inspect and
# customise them without having to parse a string first.  Builders always
# return a fresh object and never mutate these constants.
DEFAULT_AUTH_TEMPLATE: dict[str, Any] = {
    "type": "auth",
    "password": "{password}",
}
DEFAULT_SUBSCRIBE_TEMPLATE: dict[str, Any] = {
    "type": "subscribe",
    "event": "{event}",
}
DEFAULT_EVENT_TEMPLATE: dict[str, Any] = {
    "type": "event",
    "event": "{event}",
    "player": "{sender}",
    "message": "{message}",
}

_MISSING = object()
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_DOLLAR_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]+)\}")
_PATH_TOKEN_RE = re.compile(r"([^.[\\]+)|\\.")


class ProtocolError(ValueError):
    """Base class for malformed or invalid protocol frames."""


class ProtocolDecodeError(ProtocolError):
    """Raised when a WebSocket payload is not a JSON object."""


class ProtocolAuthenticationError(ProtocolError):
    """Raised when the server rejects an authentication frame."""


def _split_path(path: str) -> tuple[str, ...]:
    """Split a small dot/bracket path into mapping keys and list indexes.

    Supported forms are ``player``, ``payload.player``, ``/payload/player``
    (JSON-pointer-like), and ``payload[0].player``.  A backslash can escape a
    dot in a key.  The function is intentionally not a full JSONPath
    implementation: paths are configuration values, not expressions.
    """

    if path == "":
        return ()
    if path.startswith("/"):
        # JSON pointer escaping is useful for configurations copied from JSON
        # tooling.  Keep an empty leading segment out of the result.
        return tuple(
            part.replace("~1", "/").replace("~0", "~")
            for part in path.split("/")[1:]
            if part != ""
        )

    tokens: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(path):
        char = path[index]
        if char == "\\" and index + 1 < len(path):
            current.append(path[index + 1])
            index += 2
            continue
        if char == ".":
            if current:
                tokens.append("".join(current))
                current.clear()
            index += 1
            continue
        if char == "[":
            if current:
                tokens.append("".join(current))
                current.clear()
            end = path.find("]", index + 1)
            if end < 0:
                current.append(path[index:])
                break
            token = path[index + 1 : end].strip()
            if (
                len(token) >= 2
                and token[0] == token[-1]
                and token[0] in {"'", '"'}
            ):
                token = token[1:-1]
            if token:
                tokens.append(token)
            index = end + 1
            if index < len(path) and path[index] == ".":
                index += 1
            continue
        current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def _path_candidates(path: PathSpec | None) -> tuple[tuple[str, ...], ...]:
    if path is None:
        return ()
    if isinstance(path, str):
        return (_split_path(path),)
    # A list/tuple is treated as a list of alternative paths.  A tuple of
    # path segments is also convenient, so support it when every item is a
    # simple segment and the sequence does not name a real alternative path.
    values = tuple(str(item) for item in path)
    if not values:
        return ((),)
    # Sequences are alternative complete paths.  This is the representation
    # used by the parser defaults (for example ("player", "sender", ...)).
    # A nested path should be supplied as a dotted string or JSON pointer.
    return tuple(_split_path(item) for item in values)


def get_path(value: Any, path: PathSpec | None, default: Any = None) -> Any:
    """Return a value at a configurable path.

    ``path`` can be one path or a sequence of alternative paths.  Missing
    values return ``default`` (which may be the private ``_MISSING`` sentinel
    for callers that need to distinguish a missing value from ``None``).
    Mapping keys are preferred over sequence indexing, and numeric path
    components index lists/tuples.
    """

    candidates = _path_candidates(path)
    if not candidates:
        return value
    for parts in candidates:
        current = value
        for part in parts:
            if isinstance(current, Mapping):
                if part not in current:
                    current = _MISSING
                    break
                current = current[part]
            elif isinstance(current, (list, tuple)):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    current = _MISSING
                    break
            else:
                current = _MISSING
                break
        if current is not _MISSING:
            return current
    return default


def has_path(value: Any, path: PathSpec | None) -> bool:
    """Return whether ``path`` resolves, including values explicitly set to None."""

    return get_path(value, path, _MISSING) is not _MISSING


def set_path(value: dict[str, Any], path: str, item: Any) -> None:
    """Set a dotted path in a mapping, creating intermediate mappings.

    This helper is primarily useful for custom frame templates.  It rejects
    list indexes because protocol templates should be object-shaped and a
    silent list mutation is surprisingly easy to get wrong.
    """

    parts = _split_path(path)
    if not parts:
        raise ValueError("a frame path must not be empty")
    current = value
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = item


def _lookup_template_value(name: str, context: Mapping[str, Any]) -> Any:
    if name in context:
        return context[name]
    found = get_path(context, name, _MISSING)
    if found is not _MISSING:
        return found
    return _MISSING


def _render_string(template: str, context: Mapping[str, Any]) -> Any:
    # Preserve the type of an exact placeholder.  This lets templates use
    # ``{"enabled": "{enabled}"}`` without turning a boolean into a string.
    exact = _PLACEHOLDER_RE.fullmatch(template)
    if exact:
        value = _lookup_template_value(exact.group(1), context)
        if value is not _MISSING:
            return deepcopy(value)
    dollar_exact = _DOLLAR_PLACEHOLDER_RE.fullmatch(template)
    if dollar_exact:
        value = _lookup_template_value(dollar_exact.group(1), context)
        if value is not _MISSING:
            return deepcopy(value)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = _lookup_template_value(name, context)
        return match.group(0) if value is _MISSING else str(value)

    rendered = _PLACEHOLDER_RE.sub(replace, template)
    return _DOLLAR_PLACEHOLDER_RE.sub(replace, rendered)


def render_template(template: Any, context: Mapping[str, Any]) -> Any:
    """Recursively render placeholders in a JSON-compatible frame template.

    Both ``{name}`` and ``${name}`` placeholders are accepted.  Unknown
    placeholders are left untouched, which makes it possible to use literal
    braces in a custom template and gives the server a useful error instead
    of silently dropping a field.
    """

    if isinstance(template, str):
        return _render_string(template, context)
    if isinstance(template, Mapping):
        return {
            key: render_template(item, context)
            for key, item in template.items()
        }
    if isinstance(template, list):
        return [render_template(item, context) for item in template]
    if isinstance(template, tuple):
        return [render_template(item, context) for item in template]
    return deepcopy(template)


def _template_to_frame(template: FrameTemplate, context: Mapping[str, Any]) -> dict[str, Any]:
    rendered = render_template(template, context)
    if isinstance(rendered, str):
        try:
            rendered = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise ProtocolError("frame template rendered invalid JSON") from exc
    if not isinstance(rendered, Mapping):
        raise ProtocolError("frame template must render a JSON object")
    return dict(deepcopy(rendered))


def encode_frame(frame: Mapping[str, Any]) -> str:
    """Encode a JSON object for transmission over WebSocket."""

    if not isinstance(frame, Mapping):
        raise ProtocolError("protocol frames must be JSON objects")
    try:
        return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ProtocolError("protocol frame is not JSON serialisable") from exc


def decode_frame(payload: str | bytes | bytearray | Mapping[str, Any]) -> dict[str, Any]:
    """Decode a WebSocket payload into a JSON object.

    Mapping inputs are copied so downstream parser code cannot mutate a test
    double or a caller-owned object.  JSON arrays and scalar values are
    rejected because they cannot be MCP frames.
    """

    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolDecodeError("WebSocket payload is not UTF-8") from exc
    if not isinstance(payload, str):
        raise ProtocolDecodeError(f"unsupported WebSocket payload type: {type(payload).__name__}")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolDecodeError("WebSocket payload is not valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ProtocolDecodeError("MCP frame must be a JSON object")
    return dict(decoded)


def build_auth_frame(password: str, template: FrameTemplate = DEFAULT_AUTH_TEMPLATE) -> dict[str, Any]:
    """Build the default or configured authentication frame."""

    return _template_to_frame(template, {"password": password})


def build_subscribe_frame(
    event: str = "PlayerMessage",
    template: FrameTemplate = DEFAULT_SUBSCRIBE_TEMPLATE,
) -> dict[str, Any]:
    """Build the default or configured subscription frame."""

    return _template_to_frame(template, {"event": event, "event_name": event})


def build_event_frame(
    sender: str,
    message: str,
    event: str = "PlayerMessage",
    timestamp: str | None = None,
    template: FrameTemplate = DEFAULT_EVENT_TEMPLATE,
) -> dict[str, Any]:
    """Build a representative event frame, useful for mocks and tests."""

    return _template_to_frame(
        template,
        {
            "event": event,
            "event_name": event,
            "sender": sender,
            "player": sender,
            "message": message,
            "timestamp": timestamp,
        },
    )


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    """Configurable wire details used by :class:`mcp_client.MCPClient`.

    ``field_paths`` is shared with the parser.  It may contain keys such as
    ``event_name``, ``sender``, ``message``, ``timestamp``, ``event_id``, and
    ``kind``.  Values can be a dotted path or a sequence of fallback paths.
    """

    auth_template: FrameTemplate = field(
        default_factory=lambda: deepcopy(DEFAULT_AUTH_TEMPLATE)
    )
    subscribe_template: FrameTemplate = field(
        default_factory=lambda: deepcopy(DEFAULT_SUBSCRIBE_TEMPLATE)
    )
    event_name: str = "PlayerMessage"
    event_type_path: PathSpec = "type"
    event_name_path: PathSpec = "event"
    auth_status_path: PathSpec = "status"
    auth_success_values: tuple[Any, ...] = ("success", "ok", True)
    field_paths: Mapping[str, PathSpec] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ProtocolConfig":
        """Create a config from a flat or lightly nested mapping.

        The aliases ``auth_frame_template``/``subscribe_frame_template`` and
        ``fields``/``field_paths`` are accepted to keep INI/YAML adapters
        uncomplicated.
        """

        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        data = dict(value)
        auth = data.get("auth_template", data.get("auth_frame_template"))
        subscribe = data.get(
            "subscribe_template", data.get("subscribe_frame_template")
        )
        fields = data.get("field_paths", data.get("fields", {}))
        if not isinstance(fields, Mapping):
            raise TypeError("protocol field_paths must be a mapping")
        successes = data.get("auth_success_values", cls.auth_success_values)
        if isinstance(successes, str):
            successes = (successes,)
        else:
            successes = tuple(successes)
        return cls(
            auth_template=(deepcopy(auth) if auth is not None else deepcopy(DEFAULT_AUTH_TEMPLATE)),
            subscribe_template=(
                deepcopy(subscribe)
                if subscribe is not None
                else deepcopy(DEFAULT_SUBSCRIBE_TEMPLATE)
            ),
            event_name=str(data.get("event_name", data.get("subscribe_event", "PlayerMessage"))),
            event_type_path=data.get("event_type_path", "type"),
            event_name_path=data.get("event_name_path", "event"),
            auth_status_path=data.get("auth_status_path", "status"),
            auth_success_values=successes,
            field_paths=dict(fields),
        )

    def auth_frame(self, password: str) -> dict[str, Any]:
        return build_auth_frame(password, self.auth_template)

    def subscribe_frame(self) -> dict[str, Any]:
        return build_subscribe_frame(self.event_name, self.subscribe_template)

    def is_auth_success(self, frame: Mapping[str, Any]) -> bool:
        status = get_path(frame, self.auth_status_path, _MISSING)
        if status is _MISSING:
            # A few MCC versions put the status under ``result`` while
            # retaining the same success values.
            status = get_path(frame, ("result", "auth_status"), _MISSING)
        if status is _MISSING:
            return False
        return any(status == expected for expected in self.auth_success_values)

    def is_event(self, frame: Mapping[str, Any]) -> bool:
        event_name = get_path(frame, self.event_name_path, _MISSING)
        if event_name is not _MISSING:
            return str(event_name).casefold() == self.event_name.casefold()
        # The event marker is optional in some wrappers; an explicit event
        # type still identifies a frame, while the parser will decide whether
        # it contains a message.
        frame_type = get_path(frame, self.event_type_path, _MISSING)
        return frame_type is not _MISSING and str(frame_type).casefold() == "event"


# Compatibility aliases used by small integrations which prefer function
# names over the ``build_*`` spelling.
auth_frame = build_auth_frame
subscribe_frame = build_subscribe_frame
event_frame = build_event_frame


__all__ = [
    "DEFAULT_AUTH_TEMPLATE",
    "DEFAULT_EVENT_TEMPLATE",
    "DEFAULT_SUBSCRIBE_TEMPLATE",
    "FrameTemplate",
    "PathSpec",
    "ProtocolAuthenticationError",
    "ProtocolConfig",
    "ProtocolDecodeError",
    "ProtocolError",
    "auth_frame",
    "build_auth_frame",
    "build_event_frame",
    "build_subscribe_frame",
    "decode_frame",
    "encode_frame",
    "event_frame",
    "get_path",
    "has_path",
    "render_template",
    "set_path",
    "subscribe_frame",
]
