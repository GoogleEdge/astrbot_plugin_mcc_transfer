"""Lifecycle coordinator for the MCC transfer pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .astrbot_adapter import AstrBotSender, resolve_target
    from .control import write_status
    from .delivery import DeliveryPipeline
    from .filter_chain import FilterChain
    from .mcp_client import MCPClient
    from .models import RuntimeStatus
    from .persistence import DedupStore, RetryQueue
except ImportError:  # pragma: no cover - standalone fallback
    from astrbot_adapter import AstrBotSender, resolve_target
    from control import write_status
    from delivery import DeliveryPipeline
    from filter_chain import FilterChain
    from models import RuntimeStatus
    from persistence import DedupStore, RetryQueue

    try:
        from mcp_client import MCPClient
    except ImportError:  # pragma: no cover - protects partial installs during development
        MCPClient = None  # type: ignore[assignment,misc]

try:
    from .http_mcp_client import HTTPMCPClient
except ImportError:  # pragma: no cover - standalone fallback
    from http_mcp_client import HTTPMCPClient

LOGGER = logging.getLogger(__name__)


class PluginRuntime:
    """Own background tasks and coordinate all core components."""

    def __init__(
        self,
        config: Any,
        *,
        context: Any | None = None,
        sender: Any | None = None,
        data_dir: str | Path = "data",
    ) -> None:
        self.config = config
        self.context = context
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sender = sender
        self.status = RuntimeStatus()
        self._mcp: Any = None
        self._mcp_task: asyncio.Task[Any] | None = None
        self._retry_task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._started = False
        self._lock = asyncio.Lock()
        self.pipeline: DeliveryPipeline | None = None
        self._status_path = self.data_dir / "status.json"
        self._http_mcp: HTTPMCPClient | None = None

    def _write_status(self) -> None:
        snapshot = self.status.to_dict()
        snapshot["mcp"] = {
            "transport": self._value(self._get("mcp"), "transport", "http"),
            "url": self._value(self._get("mcp"), "url", ""),
            "password_configured": bool(str(self._value(self._get("mcp"), "password", "")).strip()),
        }
        try:
            write_status(self._status_path, snapshot)
        except Exception:
            LOGGER.exception("Failed to write MCC transfer status")

    def _get(self, name: str, default: Any = None) -> Any:
        if hasattr(self.config, name):
            return getattr(self.config, name)
        if hasattr(self.config, "get"):
            return self.config.get(name, default)
        return default

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._stop_event.clear()
            self.status.running = True
            self.status.lifecycle_state = "starting"
            self.status.last_error = None
            self._started = True
            self._write_status()
            LOGGER.info("MCC transfer runtime starting")
            try:
                self.pipeline = await self._build_pipeline()
                mcp = self._get("mcp")
                if MCPClient is None and str(self._value(mcp, "transport", "http")).casefold() == "websocket":
                    raise RuntimeError("mcp_client module is unavailable")
                client_config = mcp if mcp is not None else self.config
                on_message = self._on_mcp_message
                transport = str(self._value(client_config, "transport", "http")).casefold()
                if transport == "http":
                    self._http_mcp = HTTPMCPClient(
                        str(self._value(client_config, "url", "http://127.0.0.1:33333/mcp")),
                        timeout=float(self._value(client_config, "connect_timeout", 10)),
                        poll_interval=float(self._value(client_config, "poll_interval", 2)),
                        chat_tool=str(self._value(client_config, "chat_tool", "mcc_chat_history")),
                        chat_max_count=int(self._value(client_config, "chat_max_count", 50)),
                        on_message=self._on_http_event,
                        on_state=self._on_mcp_state,
                        logger=LOGGER,
                    )
                    self._mcp = self._http_mcp
                    self._mcp_task = asyncio.create_task(self._http_mcp.connect(), name="mcc-http-mcp-client")
                else:
                    host = self._value(client_config, "host", "127.0.0.1")
                    port = int(self._value(client_config, "port", 25575))
                    password = str(self._value(client_config, "password", ""))
                    self._mcp = MCPClient(
                        host,
                        port,
                        password,
                        on_message,
                        reconnect_initial_delay=float(self._value(client_config, "reconnect_initial_delay", 1)),
                        reconnect_max_delay=float(self._value(client_config, "reconnect_max_delay", 30)),
                        connect_timeout=float(self._value(client_config, "connect_timeout", 10)),
                        auth_timeout=float(self._value(client_config, "auth_timeout", 10)),
                        subscribe_timeout=float(self._value(client_config, "subscribe_timeout", 10)),
                        auth_mode=str(self._value(client_config, "auth_mode", "auto")),
                        subscribe_ack=bool(self._value(client_config, "subscribe_ack", False)),
                        protocol=self._get("protocol"),
                        parser=self._get("parser"),
                        event_name=self._value(self._get("protocol"), "event_name", None),
                        on_state=self._on_mcp_state,
                        logger=LOGGER,
                    )
                    self._mcp_task = asyncio.create_task(self._mcp.connect(), name="mcc-mcp-client")
                self._mcp_task.add_done_callback(self._mcp_done)
                self._retry_task = asyncio.create_task(self._retry_loop(), name="mcc-retry-worker")
                LOGGER.info("MCC transfer runtime started: transport=%s", transport)
                self._write_status()
            except Exception as exc:
                self.status.running = False
                self.status.last_error = str(exc)
                self._started = False
                self._write_status()
                LOGGER.exception("MCC transfer runtime failed to start")
                raise

    async def _on_http_event(
        self,
        sender: str | Mapping[str, Any],
        message: str | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> None:
        if isinstance(sender, Mapping):
            event = sender
            actual_sender = str(event.get("player", event.get("sender", event.get("username", "MCC"))))
            actual_message = str(event.get("message", event.get("text", event.get("content", ""))))
            actual_raw = event
        else:
            actual_sender = sender
            actual_message = str(message or "")
            actual_raw = raw or {}
        if actual_message:
            await self._on_mcp_message(actual_sender, actual_message, actual_raw)

    async def _on_mcp_state(self, state: str, details: Mapping[str, Any]) -> None:
        self.status.lifecycle_state = state
        self.status.connected = state in {"socket_open", "authenticated", "auth_skipped", "ready"}
        if self._mcp is not None:
            self.status.mcp_connection_count = int(getattr(self._mcp, "connection_count", 0))
        if state in {"failed", "disconnected", "reconnecting"}:
            self.status.last_error = str(details.get("error", details.get("reason", ""))) or self.status.last_error
        LOGGER.info("MCC MCP lifecycle: state=%s details=%s", state, dict(details))
        self._write_status()

    def _mcp_done(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled() or self._stop_event.is_set():
            return
        error = task.exception()
        if error is not None:
            self.status.failed += 1
            self.status.last_error = str(error)
            LOGGER.exception("MCC MCP task stopped unexpectedly", exc_info=error)
        self.status.connected = False
        self._write_status()

    async def _build_pipeline(self) -> DeliveryPipeline:
        config = self.config
        dedup_cfg = self._get("dedup")
        retry_cfg = self._get("retry")
        filter_cfg = self._get("filter") or self._get("filters")
        security_cfg = self._get("security")
        target_cfg = self._get("target")
        dedup = DedupStore(
            self._state_path(dedup_cfg, "state_file", "dedup_cache.json"),
            cache_size=int(self._value(dedup_cfg, "cache_size", 1000)),
            ttl_seconds=float(self._value(dedup_cfg, "ttl_seconds", 300)),
        )
        retry = RetryQueue(
            self._state_path(retry_cfg, "queue_file", "failed_messages.json"),
            max_queue_size=int(self._value(retry_cfg, "max_queue_size", 1000)),
            expire_seconds=float(self._value(retry_cfg, "message_expire_seconds", 3600)),
            drop_expired=bool(self._value(retry_cfg, "drop_expired_messages", False)),
        )
        await dedup.load()
        await retry.load()
        chain = FilterChain(filter_cfg or {}, dedup=dedup)
        if self.sender is None:
            if self.context is None:
                raise RuntimeError("context or sender is required")
            self.sender = AstrBotSender(self.context)
        target = resolve_target(target_cfg or config)
        self.status.target_umo = target.umo
        return DeliveryPipeline(
            filter_chain=chain,
            dedup=dedup,
            retry_queue=retry,
            sender=self.sender,
            target_umo=target.umo,
            formatter_config=target_cfg or config,
            security_config=security_cfg or config,
            retry_config=retry_cfg or config,
        )

    def _state_path(self, section: Any, key: str, default: str) -> Path:
        value = self._value(section, key, default)
        path = Path(str(value))
        return path if path.is_absolute() else self.data_dir / path.name

    @staticmethod
    def _value(section: Any, key: str, default: Any = None) -> Any:
        if section is None:
            return default
        if hasattr(section, key):
            return getattr(section, key)
        if hasattr(section, "get"):
            return section.get(key, default)
        return default

    async def _on_mcp_message(self, sender: str, message: str, raw: Mapping[str, Any]) -> None:
        self.status.received += 1
        self.status.last_message_at = datetime.now().astimezone().isoformat()
        if self.pipeline is None:
            return
        try:
            result = await self.pipeline.handle_raw(sender, message, raw)
            if result.sent:
                self.status.forwarded += 1
                LOGGER.info("MCC message forwarded: sender=%s", sender)
            elif result.filtered:
                self.status.filtered += 1
                LOGGER.debug("MCC message filtered: sender=%s reason=%s", sender, result.reason)
            else:
                self.status.failed += 1
                self.status.last_error = str(result.error or result.reason)
                LOGGER.warning("MCC message delivery failed: reason=%s error=%s", result.reason, result.error)
            self._write_status()
        except Exception as exc:  # callback failures must not kill MCP receive loop
            self.status.failed += 1
            self.status.last_error = str(exc)
            LOGGER.exception("Failed to process MCP message")

    async def _retry_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.pipeline is not None:
                    results = await self.pipeline.process_due_retries()
                    for result in results:
                        self.status.retried += 1
                        if result.sent:
                            self.status.forwarded += 1
                        elif result.error is not None:
                            self.status.failed += 1
                            self.status.last_error = str(result.error)
                    if results:
                        self._write_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.failed += 1
                self.status.last_error = str(exc)
                self._write_status()
                LOGGER.exception("Retry worker failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self._stop_event.set()
            for task in (self._mcp_task, self._retry_task):
                if task is not None:
                    task.cancel()
            for task in (self._mcp_task, self._retry_task):
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if self._mcp is not None:
                with contextlib.suppress(Exception):
                    await self._mcp.disconnect()
            self._mcp = None
            self._http_mcp = None
            self._mcp_task = None
            self._retry_task = None
            self._started = False
            self.status.running = False
            self.status.connected = False
            LOGGER.info("MCC transfer runtime stopped")
            self._write_status()

    async def reload(self, config: Any) -> None:
        await self.stop()
        self.config = config
        await self.start()

    def snapshot(self) -> dict[str, Any]:
        return self.status.to_dict()
