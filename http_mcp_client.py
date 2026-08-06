"""HTTP JSON-RPC client for the embedded MCC MCP server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib import error, request

LOGGER = logging.getLogger(__name__)


class HTTPMCPError(RuntimeError):
    """Raised when an HTTP MCP request or response is invalid."""


class HTTPMCPClient:
    """Small dependency-free client for MCC's streamable HTTP MCP endpoint."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        poll_interval: float = 2.0,
        chat_tool: str = "mcc_recent_events",
        chat_max_count: int = 50,
        on_message: Callable[..., Any] | None = None,
        on_state: Callable[[str, Mapping[str, Any]], Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("HTTP MCP URL must start with http:// or https://")
        self.url = url
        self.timeout = max(0.1, float(timeout))
        self.poll_interval = max(0.0, float(poll_interval))
        self.chat_tool = str(chat_tool)
        self.chat_max_count = max(1, int(chat_max_count))
        self.on_message = on_message
        self.on_state = on_state
        self.logger = logger or LOGGER
        self.session_id: str | None = None
        self.request_id = 0
        self.initialized = False
        self.connected = False
        self.last_error: BaseException | None = None
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None
        self._last_event_id = 0

    async def _state(self, name: str, **details: Any) -> None:
        self.logger.info("MCC HTTP MCP state=%s details=%s", name, details)
        if self.on_state is None:
            return
        result = self.on_state(name, details)
        if asyncio.iscoroutine(result):
            await result

    async def _request(self, payload: Mapping[str, Any], *, session_id: str | None = None) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        def send() -> tuple[int, Mapping[str, Any], bytes]:
            req = request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    response_headers = {str(k): str(v) for k, v in response.headers.items()}
                    return response.status, response_headers, response.read()
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise HTTPMCPError(f"HTTP MCP returned {exc.code}: {detail[:500]}") from exc
            except (error.URLError, TimeoutError, OSError) as exc:
                raise HTTPMCPError(f"HTTP MCP request failed: {exc}") from exc

        status, response_headers, raw = await asyncio.to_thread(send)
        for key, value in response_headers.items():
            if key.casefold() == "mcp-session-id" and value:
                self.session_id = value
                break
        if status < 200 or status >= 300:
            raise HTTPMCPError(f"HTTP MCP returned status {status}")
        content = raw.decode("utf-8", errors="replace").strip()
        if not content:
            return None
        data_lines = [line[6:].strip() for line in content.splitlines() if line.startswith("data:")]
        candidate = "\n".join(data_lines) if data_lines else content
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise HTTPMCPError(f"HTTP MCP returned non-JSON response: {candidate[:500]}") from exc

    async def initialize(self) -> Mapping[str, Any]:
        self.request_id += 1
        response = await self._request(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "astrbot-plugin-mcc-transfer", "version": "1.0"},
                },
            }
        )
        if not isinstance(response, Mapping):
            raise HTTPMCPError("MCP initialize response is not an object")
        self.session_id = self.session_id or str(response.get("session_id", "")) or None
        self.initialized = True
        self.connected = True
        await self._request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=self.session_id,
        )
        return response

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        if not self.initialized:
            await self.initialize()
        self.request_id += 1
        response = await self._request(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments or {})},
            },
            session_id=self.session_id,
        )
        if not isinstance(response, Mapping):
            raise HTTPMCPError("MCP tool response is not an object")
        result = response.get("result", response)
        if isinstance(result, Mapping):
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], Mapping):
                text = content[0].get("text")
                if isinstance(text, str):
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text
        return result

    async def list_tools(self) -> Any:
        if not self.initialized:
            await self.initialize()
        self.request_id += 1
        return await self._request(
            {"jsonrpc": "2.0", "id": self.request_id, "method": "tools/list"},
            session_id=self.session_id,
        )

    @classmethod
    def _events_from_result(cls, result: Any) -> list[Mapping[str, Any]]:
        if isinstance(result, Mapping):
            for key in ("events", "items", "recentEvents"):
                value = result.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
            for key in ("data", "result"):
                value = result.get(key)
                if isinstance(value, (Mapping, list)):
                    nested = cls._events_from_result(value)
                    if nested:
                        return nested
            return [result] if any(key in result for key in ("message", "text", "event")) else []
        if isinstance(result, list):
            return [item for item in result if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _event_message(event: Mapping[str, Any]) -> tuple[str, str] | None:
        """Extract chat text from MCC recent-event envelopes."""
        event_type = str(event.get("type", event.get("event", ""))).casefold()
        data = event.get("data", {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = {"text": data}
        if not isinstance(data, Mapping):
            data = {}
        if event_type in {"chat_public", "onchatpublic", "chat_private", "onchatprivate"}:
            sender = str(data.get("sender", data.get("username", "MCC")))
            message = str(data.get("message", data.get("text", "")))
            return (sender, message) if message else None
        if event_type in {"chat_raw", "onchatraw"}:
            message = str(data.get("text", data.get("message", "")))
            return ("MCC", message) if message else None
        # Accept already-normalized/custom event records too.
        sender = event.get("player", event.get("sender"))
        message = event.get("message", event.get("text", event.get("content")))
        if message is not None:
            return str(sender or "MCC"), str(message)
        return None

    async def poll_once(self) -> int:
        result = await self.call_tool(
            self.chat_tool,
            {"afterId": self._last_event_id, "maxCount": self.chat_max_count},
        )
        count = 0
        for event in self._events_from_result(result):
            event_id = event.get("id", event.get("eventId"))
            try:
                if event_id is not None:
                    self._last_event_id = max(self._last_event_id, int(event_id))
            except (TypeError, ValueError):
                pass
            parsed = self._event_message(event)
            if parsed is None or self.on_message is None:
                continue
            try:
                callback_result = self.on_message(parsed[0], parsed[1], event)
            except TypeError:
                callback_result = self.on_message(event)
            if asyncio.iscoroutine(callback_result):
                await callback_result
            count += 1
        return count

    async def connect(self) -> None:
        self._stop_event.clear()
        self.last_error = None
        self.logger.info("MCC HTTP MCP connecting: url=%s tool=%s", self.url, self.chat_tool)
        await self._state("connecting", url=self.url)
        try:
            await self.initialize()
            await self._state("ready", session_id=self.session_id or "assigned-by-server")
            self.logger.info("MCC HTTP MCP ready: session=%s", self.session_id or "assigned-by-server")
            while not self._stop_event.is_set():
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = exc
                    await self._state("poll_failed", error=str(exc))
                    self.logger.warning("MCC HTTP MCP poll failed: %s", exc)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.connected = False
            self.initialized = False
            await self._state("stopped")
            self.logger.info("MCC HTTP MCP stopped")

    async def start(self) -> asyncio.Task[Any]:
        if self._task is None:
            self._task = asyncio.create_task(self.connect(), name="mcc-http-mcp-client")
        return self._task

    async def disconnect(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self.connected = False


__all__ = ["HTTPMCPClient", "HTTPMCPError"]
