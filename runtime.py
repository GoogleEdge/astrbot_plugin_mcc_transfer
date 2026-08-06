"""Lifecycle coordinator for the MCC transfer pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot_adapter import AstrBotSender, resolve_target
from delivery import DeliveryPipeline
from filter_chain import FilterChain
from models import RuntimeStatus
from persistence import DedupStore, RetryQueue

try:
    from mcp_client import MCPClient
except ImportError:  # pragma: no cover - protects partial installs during development
    MCPClient = None  # type: ignore[assignment,misc]

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
            self._started = True
            try:
                self.pipeline = await self._build_pipeline()
                if MCPClient is None:
                    raise RuntimeError("mcp_client module is unavailable")
                mcp = self._get("mcp")
                client_config = mcp if mcp is not None else self.config
                on_message = self._on_mcp_message
                host = self._value(client_config, "host", "127.0.0.1")
                port = int(self._value(client_config, "port", 25575))
                password = str(self._value(client_config, "password", ""))
                self._mcp = MCPClient(host, port, password, on_message)
                self._mcp_task = asyncio.create_task(self._mcp.connect(), name="mcc-mcp-client")
                self._retry_task = asyncio.create_task(self._retry_loop(), name="mcc-retry-worker")
            except Exception:
                self.status.running = False
                self._started = False
                raise

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
            if result:
                self.status.forwarded += 1
            else:
                self.status.filtered += 1
        except Exception as exc:  # callback failures must not kill MCP receive loop
            self.status.failed += 1
            self.status.last_error = str(exc)
            LOGGER.exception("Failed to process MCP message")

    async def _retry_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.pipeline is not None:
                    await self.pipeline.process_due_retries()
            except asyncio.CancelledError:
                raise
            except Exception:
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
            self._mcp_task = None
            self._retry_task = None
            self._started = False
            self.status.running = False
            self.status.connected = False

    async def reload(self, config: Any) -> None:
        await self.stop()
        self.config = config
        await self.start()

    def snapshot(self) -> dict[str, Any]:
        return self.status.to_dict()
