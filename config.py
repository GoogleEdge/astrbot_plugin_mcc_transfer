"""Configuration models and loaders for native AstrBot and standalone INI use.

AstrBot supplies a dictionary-like ``AstrBotConfig``.  The standalone CLI uses
an INI file.  Both are normalized into the dataclasses in this module so the
runtime never has to guess whether a value came from a WebUI or an INI file.
"""

from __future__ import annotations

import configparser
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """Raised when plugin configuration is invalid."""


_TRUE = {"1", "true", "yes", "on", "y", "enabled"}
_FALSE = {"0", "false", "no", "off", "n", "", "disabled"}


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _list(value: Any, *, separators: str = ",\n") -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        pattern = "[" + re.escape(separators) + "]+"
        values = re.split(pattern, value)
    elif isinstance(value, Mapping):
        values = list(value.keys())
    else:
        try:
            values = list(value)
        except TypeError:
            values = [value]
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _json_value(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON configuration value: {value!r}") from exc


def _env(value: Any) -> str:
    text = "" if value is None else str(value)
    return os.path.expandvars(text).strip()


def _get(section: Any, key: str, default: Any = None) -> Any:
    if section is None:
        return default
    if isinstance(section, Mapping):
        return section.get(key, default)
    value = getattr(section, key, default)
    return value


def _section(root: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = root.get(name, {})
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class MCPSettings:
    # Current MCC exposes an HTTP MCP endpoint.  The legacy WebSocket profile
    # remains available by setting transport=websocket.
    transport: str = "http"
    url: str = "http://127.0.0.1:33333/mcp"
    host: str = "127.0.0.1"
    port: int = 33333
    password: str = ""
    auth_mode: str = "auto"
    subscribe_ack: bool = False
    poll_interval: float = 2.0
    chat_tool: str = "mcc_recent_events"
    chat_max_count: int = 50
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 30.0
    connect_timeout: float = 10.0
    auth_timeout: float = 10.0
    subscribe_timeout: float = 10.0


@dataclass(frozen=True, slots=True)
class TargetSettings:
    platform_name: str = "qq_official"
    platform_instance: str = "default"
    message_type: str = "GroupMessage"
    group_id: str = ""
    # Empty means use AstrBot's native UMO format.  A custom template remains
    # available for adapters with a project-specific session convention.
    umo_template: str = ""
    umo_override: str = ""
    message_template: str = "[Minecraft] <{sender}> {message}"

    @property
    def target_umo(self) -> str:
        if self.umo_override:
            return self.umo_override
        if not self.umo_template:
            return f"{self.platform_name}:{self.message_type}:{self.group_id}"
        return self.umo_template.format(
            platform_name=self.platform_name,
            platform_instance=self.platform_instance,
            message_type=self.message_type,
            group_id=self.group_id,
        )


@dataclass(frozen=True, slots=True)
class FilterSettings:
    enabled: bool = True
    ignore_bot_messages: bool = True
    bot_name: str = "MCCBot"
    ignore_system_messages: bool = True
    ignore_join_messages: bool = True
    ignore_leave_messages: bool = True
    ignore_death_messages: bool = True
    ignore_advancement_messages: bool = True
    ignore_server_announcements: bool = True
    ignore_empty_messages: bool = True
    ignore_command_messages: bool = True
    command_prefixes: tuple[str, ...] = ("/", "!")
    enable_player_blacklist: bool = False
    blacklist_players: tuple[str, ...] = ()
    enable_player_whitelist: bool = False
    whitelist_players: tuple[str, ...] = ()
    enable_keyword_filter: bool = False
    blocked_keywords: tuple[str, ...] = ()
    keyword_case_sensitive: bool = False
    keyword_whole_word: bool = False
    enable_regex_filter: bool = False
    blocked_regex: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DedupSettings:
    enabled: bool = True
    cache_size: int = 1000
    ttl_seconds: float = 300.0
    state_file: str = "data/dedup_cache.json"


@dataclass(frozen=True, slots=True)
class RetrySettings:
    max_attempts: int = 5
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    queue_file: str = "data/failed_messages.json"
    replay_failed_messages: bool = True
    max_queue_size: int = 1000
    drop_expired_messages: bool = False
    message_expire_seconds: float = 3600.0


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    max_message_length: int = 500
    rate_limit_per_second: float = 5.0
    split_long_messages: bool = True
    merge_messages: bool = False
    merge_window_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str = "INFO"
    log_file: str = ""
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass(frozen=True, slots=True)
class ProtocolSettings:
    auth_template: Any = field(default_factory=lambda: {"type": "auth", "password": "{password}"})
    subscribe_template: Any = field(default_factory=lambda: {"type": "subscribe", "event": "{event}"})
    event_name: str = "PlayerMessage"
    event_type_path: str = "type"
    event_name_path: str = "event"
    auth_status_path: str = "status"
    auth_success_values: tuple[Any, ...] = ("success", "ok", True)
    field_paths: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "auth_template": self.auth_template,
            "subscribe_template": self.subscribe_template,
            "event_name": self.event_name,
            "event_type_path": self.event_type_path,
            "event_name_path": self.event_name_path,
            "auth_status_path": self.auth_status_path,
            "auth_success_values": self.auth_success_values,
            "field_paths": dict(self.field_paths),
        }


@dataclass(frozen=True, slots=True)
class ParserSettings:
    field_paths: Mapping[str, Any] = field(default_factory=dict)
    accepted_event_names: tuple[str, ...] = ()
    ignore_unknown_events: bool = True
    allow_missing_sender: bool = False
    allow_missing_message: bool = False
    require_event_marker: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "field_paths": dict(self.field_paths),
            "accepted_event_names": self.accepted_event_names,
            "ignore_unknown_events": self.ignore_unknown_events,
            "allow_missing_sender": self.allow_missing_sender,
            "allow_missing_message": self.allow_missing_message,
            "require_event_marker": self.require_event_marker,
        }


@dataclass(frozen=True, slots=True)
class AppConfig:
    mcp: MCPSettings = field(default_factory=MCPSettings)
    target: TargetSettings = field(default_factory=TargetSettings)
    filter: FilterSettings = field(default_factory=FilterSettings)
    dedup: DedupSettings = field(default_factory=DedupSettings)
    retry: RetrySettings = field(default_factory=RetrySettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    protocol: ProtocolSettings = field(default_factory=ProtocolSettings)
    parser: ParserSettings = field(default_factory=ParserSettings)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | "AppConfig" | None,
        *,
        require_target: bool = True,
    ) -> "AppConfig":
        if isinstance(value, cls):
            return value
        root = dict(value or {})
        # AstrBot can expose either nested section objects or a flat mapping;
        # accept both without silently treating unrelated values as settings.
        sections = {name: _section(root, name) for name in (
            "mcp", "target", "filter", "dedup", "retry", "security", "logging", "protocol", "parser"
        )}
        for name in sections:
            if not sections[name]:
                flat = {key: item for key, item in root.items() if key.startswith(name + ".")}
                if flat:
                    sections[name] = {key.split(".", 1)[1]: item for key, item in flat.items()}

        m = sections["mcp"]
        t = sections["target"]
        f = sections["filter"]
        d = sections["dedup"]
        r = sections["retry"]
        s = sections["security"]
        logging_section = sections["logging"]
        p = sections["protocol"]
        q = sections["parser"]

        protocol_fields = _json_value(p.get("field_paths_json"), p.get("field_paths", {}))
        if not isinstance(protocol_fields, Mapping):
            raise ConfigError("protocol.field_paths must be a mapping")
        parser_fields = _json_value(q.get("field_paths_json"), q.get("field_paths", {}))
        if not isinstance(parser_fields, Mapping):
            raise ConfigError("parser.field_paths must be a mapping")
        auth_template = _json_value(p.get("auth_template_json"), p.get("auth_template"))
        subscribe_template = _json_value(p.get("subscribe_template_json"), p.get("subscribe_template"))
        success_values = _json_value(p.get("auth_success_values_json"), p.get("auth_success_values", ("success", "ok", True)))
        if isinstance(success_values, str):
            success_values = _list(success_values)
        else:
            success_values = tuple(success_values)

        result = cls(
            mcp=MCPSettings(
                transport=str(m.get("transport", "http")).strip().casefold(),
                url=str(m.get("url", "http://127.0.0.1:33333/mcp")).strip(),
                host=str(m.get("host", "127.0.0.1")).strip(),
                port=_int(m.get("port", 33333), 33333),
                password=_env(m.get("password", "")),
                auth_mode=str(m.get("auth_mode", "auto")).strip().casefold(),
                subscribe_ack=_bool(m.get("subscribe_ack", False)),
                poll_interval=_float(m.get("poll_interval", 2), 2),
                chat_tool=str(m.get("chat_tool", "mcc_recent_events")).strip(),
                chat_max_count=_int(m.get("chat_max_count", 50), 50),
                reconnect_initial_delay=_float(m.get("reconnect_initial_delay", 1), 1),
                reconnect_max_delay=_float(m.get("reconnect_max_delay", 30), 30),
                connect_timeout=_float(m.get("connect_timeout", 10), 10),
                auth_timeout=_float(m.get("auth_timeout", 10), 10),
                subscribe_timeout=_float(m.get("subscribe_timeout", 10), 10),
            ),
            target=TargetSettings(
                platform_name=str(t.get("platform_name", "qq_official")).strip(),
                platform_instance=str(t.get("platform_instance", "default")).strip(),
                message_type=str(t.get("message_type", "GroupMessage")).strip(),
                group_id=str(t.get("group_id", "")).strip(),
                umo_template=str(t.get("umo_template", "") or "").strip(),
                umo_override=str(t.get("umo_override", t.get("umo", "")) or "").strip(),
                message_template=str(t.get("message_template", "[Minecraft] <{sender}> {message}")),
            ),
            filter=FilterSettings(
                enabled=_bool(f.get("enabled", True), True),
                ignore_bot_messages=_bool(f.get("ignore_bot_messages", True), True),
                bot_name=str(f.get("bot_name", "MCCBot") or "").strip(),
                ignore_system_messages=_bool(f.get("ignore_system_messages", True), True),
                ignore_join_messages=_bool(f.get("ignore_join_messages", True), True),
                ignore_leave_messages=_bool(f.get("ignore_leave_messages", True), True),
                ignore_death_messages=_bool(f.get("ignore_death_messages", True), True),
                ignore_advancement_messages=_bool(f.get("ignore_advancement_messages", True), True),
                ignore_server_announcements=_bool(f.get("ignore_server_announcements", True), True),
                ignore_empty_messages=_bool(f.get("ignore_empty_messages", True), True),
                ignore_command_messages=_bool(f.get("ignore_command_messages", True), True),
                command_prefixes=tuple(_list(f.get("command_prefixes", ("/", "!")))),
                enable_player_blacklist=_bool(f.get("enable_player_blacklist", False)),
                blacklist_players=tuple(_list(f.get("blacklist_players", ()))),
                enable_player_whitelist=_bool(f.get("enable_player_whitelist", False)),
                whitelist_players=tuple(_list(f.get("whitelist_players", ()))),
                enable_keyword_filter=_bool(f.get("enable_keyword_filter", False)),
                blocked_keywords=tuple(_list(f.get("blocked_keywords", ()))),
                keyword_case_sensitive=_bool(f.get("keyword_case_sensitive", False)),
                keyword_whole_word=_bool(f.get("keyword_whole_word", False)),
                enable_regex_filter=_bool(f.get("enable_regex_filter", False)),
                blocked_regex=tuple(_list(f.get("blocked_regex", ()))),
            ),
            dedup=DedupSettings(
                enabled=_bool(d.get("enabled", True), True),
                cache_size=_int(d.get("cache_size", 1000), 1000),
                ttl_seconds=_float(d.get("ttl_seconds", 300), 300),
                state_file=str(d.get("state_file", "data/dedup_cache.json")),
            ),
            retry=RetrySettings(
                max_attempts=_int(r.get("max_attempts", 5), 5),
                initial_delay_seconds=_float(r.get("initial_delay_seconds", 1), 1),
                max_delay_seconds=_float(r.get("max_delay_seconds", 30), 30),
                queue_file=str(r.get("queue_file", "data/failed_messages.json")),
                replay_failed_messages=_bool(r.get("replay_failed_messages", True), True),
                max_queue_size=_int(r.get("max_queue_size", 1000), 1000),
                drop_expired_messages=_bool(r.get("drop_expired_messages", False)),
                message_expire_seconds=_float(r.get("message_expire_seconds", 3600), 3600),
            ),
            security=SecuritySettings(
                max_message_length=_int(s.get("max_message_length", 500), 500),
                rate_limit_per_second=_float(s.get("rate_limit_per_second", 5), 5),
                split_long_messages=_bool(s.get("split_long_messages", True), True),
                merge_messages=_bool(s.get("merge_messages", False)),
                merge_window_seconds=_float(s.get("merge_window_seconds", 3), 3),
            ),
            logging=LoggingSettings(
                level=str(logging_section.get("level", "INFO")).upper(),
                log_file=str(logging_section.get("log_file", "")),
                max_bytes=_int(logging_section.get("max_bytes", 10 * 1024 * 1024), 10 * 1024 * 1024),
                backup_count=_int(logging_section.get("backup_count", 5), 5),
            ),
            protocol=ProtocolSettings(
                auth_template=auth_template if auth_template is not None else {"type": "auth", "password": "{password}"},
                subscribe_template=subscribe_template if subscribe_template is not None else {"type": "subscribe", "event": "{event}"},
                event_name=str(p.get("event_name", "PlayerMessage")),
                event_type_path=str(p.get("event_type_path", "type")),
                event_name_path=str(p.get("event_name_path", "event")),
                auth_status_path=str(p.get("auth_status_path", "status")),
                auth_success_values=tuple(success_values),
                field_paths=dict(protocol_fields),
            ),
            parser=ParserSettings(
                field_paths=dict(parser_fields),
                accepted_event_names=tuple(_list(_json_value(q.get("accepted_event_names_json"), q.get("accepted_event_names", ())))),
                ignore_unknown_events=_bool(q.get("ignore_unknown_events", True), True),
                allow_missing_sender=_bool(q.get("allow_missing_sender", False)),
                allow_missing_message=_bool(q.get("allow_missing_message", False)),
                require_event_marker=_bool(q.get("require_event_marker", False)),
            ),
        )
        result.validate(require_target=require_target)
        return result

    @classmethod
    def from_ini(cls, path: str | Path) -> "AppConfig":
        parser = configparser.ConfigParser(interpolation=None)
        source = Path(path)
        with source.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
        sections: dict[str, dict[str, Any]] = {name: dict(parser[name]) for name in parser.sections()}
        for section in ("protocol", "parser"):
            current = sections.get(section, {})
            # The example uses JSON suffixes to retain structured values in INI.
            for key, value in list(current.items()):
                if key.endswith("_json"):
                    current[key] = value
        return cls.from_mapping(sections)

    def validate(self, *, require_target: bool = True) -> None:
        if self.mcp.transport not in {"http", "websocket"}:
            raise ConfigError("mcp.transport must be http or websocket")
        if self.mcp.transport == "http" and not self.mcp.url:
            raise ConfigError("mcp.url is required for HTTP transport")
        if not self.mcp.host:
            raise ConfigError("mcp.host is required")
        if not 1 <= self.mcp.port <= 65535:
            raise ConfigError("mcp.port must be between 1 and 65535")
        for name, value in (
            ("reconnect_initial_delay", self.mcp.reconnect_initial_delay),
            ("reconnect_max_delay", self.mcp.reconnect_max_delay),
            ("connect_timeout", self.mcp.connect_timeout),
            ("auth_timeout", self.mcp.auth_timeout),
            ("subscribe_timeout", self.mcp.subscribe_timeout),
        ):
            if value < 0:
                raise ConfigError(f"mcp.{name} must be non-negative")
        if self.mcp.reconnect_max_delay < self.mcp.reconnect_initial_delay:
            raise ConfigError("mcp.reconnect_max_delay must be >= reconnect_initial_delay")
        if self.mcp.auth_mode not in {"auto", "required", "none"}:
            raise ConfigError("mcp.auth_mode must be auto, required, or none")
        if self.mcp.auth_mode == "required" and not self.mcp.password:
            raise ConfigError("mcp.password is required when mcp.auth_mode=required")
        if self.mcp.poll_interval < 0 or self.mcp.chat_max_count < 1:
            raise ConfigError("mcp.poll_interval must be non-negative and chat_max_count positive")
        if require_target and not self.target.group_id and not self.target.umo_override:
            raise ConfigError("target.group_id is required")
        if self.target.message_type == "":
            raise ConfigError("target.message_type is required")
        try:
            self.target.message_template.format(sender="", message="", timestamp="", kind="")
        except (KeyError, ValueError, IndexError) as exc:
            raise ConfigError(f"invalid target.message_template: {exc}") from exc
        if self.dedup.cache_size < 0 or self.dedup.ttl_seconds < 0:
            raise ConfigError("dedup cache_size and ttl_seconds must be non-negative")
        if self.retry.max_attempts < 1 or self.retry.max_queue_size < 0:
            raise ConfigError("retry max_attempts must be >= 1 and max_queue_size non-negative")
        if self.retry.initial_delay_seconds < 0 or self.retry.max_delay_seconds < self.retry.initial_delay_seconds:
            raise ConfigError("retry delays are invalid")
        if self.security.max_message_length < 1:
            raise ConfigError("security.max_message_length must be positive")
        if self.security.rate_limit_per_second < 0 or self.security.merge_window_seconds < 0:
            raise ConfigError("security limits must be non-negative")
        if self.logging.max_bytes < 0 or self.logging.backup_count < 0:
            raise ConfigError("logging rotation values must be non-negative")
        for expression in self.filter.blocked_regex:
            try:
                re.compile(expression)
            except re.error as exc:
                raise ConfigError(f"invalid filter.blocked_regex {expression!r}: {exc}") from exc
        allowed = {"sender", "message", "timestamp", "kind"}
        for match in re.finditer(r"\{([^{}]+)\}", self.target.message_template):
            if match.group(1) not in allowed:
                raise ConfigError(f"unknown message template field: {match.group(1)}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Names used by integrations that prefer shorter settings labels.
McpConfig = MCPSettings
TargetConfig = TargetSettings
FilterConfig = FilterSettings
DedupConfig = DedupSettings
RetryConfig = RetrySettings
SecurityConfig = SecuritySettings
LoggingConfig = LoggingSettings

__all__ = [
    "AppConfig", "ConfigError", "MCPSettings", "McpConfig", "TargetSettings", "TargetConfig",
    "FilterSettings", "FilterConfig", "DedupSettings", "DedupConfig", "RetrySettings", "RetryConfig",
    "SecuritySettings", "SecurityConfig", "LoggingSettings", "LoggingConfig", "ProtocolSettings",
    "ParserSettings",
]
