"""Async WebSocket client for the MCC MCP server.

The client owns connection lifecycle only.  Incoming event frames are passed
through :class:`parser.MessageParser` and delivered to the supplied callback
as ``(sender, message, raw)`` for compatibility with the SPEC.  Callbacks
which accept a ParsedMessage, or async callbacks, are supported as well.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, TypeAlias

try:
    import websockets
    from websockets.exceptions import ConnectionClosed, WebSocketException
except ImportError:  # pragma: no cover - import error is raised on connect
    websockets = None  # type: ignore[assignment]

    class ConnectionClosed(Exception):
        pass

    class WebSocketException(Exception):
        pass

try:
    from .models import ParsedMessage
    from .parser import MessageParser, ParserConfig
    from .protocol import (
        ProtocolAuthenticationError,
        ProtocolConfig,
        ProtocolDecodeError,
        decode_frame,
        encode_frame,
    )
except ImportError:  # pragma: no cover - standalone fallback
    from models import ParsedMessage
    from parser import MessageParser, ParserConfig
    from protocol import (
        ProtocolAuthenticationError,
        ProtocolConfig,
        ProtocolDecodeError,
        decode_frame,
        encode_frame,
    )

MessageCallback: TypeAlias = Callable[..., Any]


class MCPClientError(RuntimeError):
    """Base exception for MCP client lifecycle failures."""


class MCPClientClosed(MCPClientError):
    """Raised when a receive loop is stopped by an explicit disconnect."""


class MCPAuthenticationError(MCPClientError, ProtocolAuthenticationError):
    """Raised when the MCP server rejects authentication."""


class MCPConnectionError(MCPClientError):
    """Raised for an initial connection failure when reconnect is disabled."""


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "success", "ok"}
    return bool(value)


def _status_is_success(frame: Mapping[str, Any], protocol: ProtocolConfig) -> bool:
    if protocol.is_auth_success(frame):
        return True
    # Some servers return ``{"ok": true}`` or ``{"authenticated": true}``
    # instead of a status string.
    for key in ("ok", "authenticated", "authorized", "success"):
        if key in frame and _coerce_bool(frame[key]):
            return True
    result = frame.get("result")
    if isinstance(result, Mapping):
        for key in ("ok", "authenticated", "authorized", "success"):
            if key in result and _coerce_bool(result[key]):
                return True
        if protocol.is_auth_success(result):
            return True
    return False


def _status_is_failure(frame: Mapping[str, Any]) -> bool:
    for key in ("status", "error", "message"):
        value = frame.get(key)
        if isinstance(value, str) and value.casefold() in {
            "error",
            "failed",
            "failure",
            "invalid",
            "unauthorized",
            "denied",
            "authentication_failed",
        }:
            return True
    if frame.get("ok") is False or frame.get("authenticated") is False:
        return True
    return False


class MCPClient:
    """Connect to an MCC MCP WebSocket server and dispatch PlayerMessage events.

    Parameters are intentionally compatible with the SPEC constructor.  Extra
    keyword arguments configure reconnection and protocol details:

    ``reconnect_initial_delay`` / ``reconnect_max_delay``
        Exponential backoff bounds in seconds.  Set ``reconnect=False`` to
        propagate the first connection error instead of retrying.
    ``protocol`` / ``parser``
        ``ProtocolConfig``/``ParserConfig`` instances or mappings.  The
        parser's field paths control event extraction; protocol templates
        control auth and subscribe frame rendering.
    ``uri`` / ``url``
        Explicit WebSocket URL override.  Otherwise ``ws://host:port`` is
        used as required by SPEC.
    ``websocket_kwargs``
        Extra keyword arguments passed to ``websockets.connect``.

    ``connect`` runs until :meth:`disconnect` is called.  It reconnects after
    unexpected socket closure and resets the backoff after a successful
    authenticated connection.  The callback may be sync or async.  A callback
    accepting one positional argument receives a ``ParsedMessage``; callbacks
    accepting three receive the SPEC tuple ``sender, message, raw``.  A
    callback with ``*args`` receives the three-argument form.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        on_message: MessageCallback,
        *,
        reconnect: bool = True,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        protocol: ProtocolConfig | Mapping[str, Any] | None = None,
        parser: MessageParser | ParserConfig | Mapping[str, Any] | None = None,
        parser_config: ParserConfig | Mapping[str, Any] | None = None,
        uri: str | None = None,
        url: str | None = None,
        event_name: str | None = None,
        websocket_kwargs: Mapping[str, Any] | None = None,
        connect_timeout: float | None = 10.0,
        logger: logging.Logger | None = None,
        **kwargs: Any,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        if not callable(on_message):
            raise TypeError("on_message must be callable")
        self.host = host
        self.port = port
        self.password = password
        self.on_message = on_message
        self.reconnect = bool(reconnect)
        self.reconnect_initial_delay = max(0.0, float(reconnect_initial_delay))
        self.reconnect_max_delay = max(
            self.reconnect_initial_delay,
            float(reconnect_max_delay),
        )
        if uri is not None and url is not None and uri != url:
            raise ValueError("uri and url disagree")
        self.uri = uri or url or f"ws://{host}:{port}"
        self.connect_timeout = connect_timeout
        self.websocket_kwargs = dict(websocket_kwargs or {})
        self.logger = logger or logging.getLogger(__name__)
        self.protocol = ProtocolConfig.from_mapping(protocol)
        if event_name is not None:
            protocol_values = {
                "auth_template": self.protocol.auth_template,
                "subscribe_template": self.protocol.subscribe_template,
                "event_name": event_name,
                "event_type_path": self.protocol.event_type_path,
                "event_name_path": self.protocol.event_name_path,
                "auth_status_path": self.protocol.auth_status_path,
                "auth_success_values": self.protocol.auth_success_values,
                "field_paths": self.protocol.field_paths,
            }
            self.protocol = ProtocolConfig.from_mapping(protocol_values)
        parser_value = parser if parser is not None else parser_config
        if isinstance(parser_value, MessageParser):
            self.parser = parser_value
        elif isinstance(parser_value, ParserConfig):
            self.parser = MessageParser(parser_value)
        else:
            parser_mapping = dict(parser_value or {})
            if event_name is not None:
                parser_mapping.setdefault("event_name", event_name)
            # Protocol field paths are useful defaults for parser clients,
            # while explicit parser paths always win.
            if self.protocol.field_paths:
                merged = dict(self.protocol.field_paths)
                merged.update(parser_mapping.get("field_paths", {}))
                parser_mapping["field_paths"] = merged
            self.parser = MessageParser(parser_mapping)
        self._extra_options = dict(kwargs)
        self._ws: Any = None
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._disconnect_requested = False
        self._authenticated = False
        self._connection_count = 0
        self._last_error: BaseException | None = None

    @property
    def websocket(self) -> Any:
        """The active WebSocket object, or ``None`` when disconnected."""

        return self._ws

    @property
    def connected(self) -> bool:
        return self._connected_event.is_set()

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def connection_count(self) -> int:
        return self._connection_count

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    def _connect_kwargs(self) -> dict[str, Any]:
        options = dict(self.websocket_kwargs)
        if self.connect_timeout is not None and "open_timeout" not in options:
            options["open_timeout"] = self.connect_timeout
        return options

    async def _open(self) -> Any:
        if websockets is None:
            raise MCPConnectionError(
                "websockets is required; install the project websocket dependency"
            )
        return await websockets.connect(self.uri, **self._connect_kwargs())

    async def _send_frame(self, frame: Mapping[str, Any]) -> None:
        if self._ws is None:
            raise MCPClientClosed("MCP client is not connected")
        await self._ws.send(encode_frame(frame))

    async def _receive_frame(self) -> dict[str, Any]:
        if self._ws is None:
            raise MCPClientClosed("MCP client is not connected")
        payload = await self._ws.recv()
        return decode_frame(payload)

    async def _authenticate(self) -> None:
        await self._send_frame(self.protocol.auth_frame(self.password))
        frame = await self._receive_frame()
        if _status_is_success(frame, self.protocol):
            self._authenticated = True
            return
        if _status_is_failure(frame) or frame:
            detail = frame.get("message", frame.get("error", frame.get("status", frame)))
            raise MCPAuthenticationError(f"MCP authentication failed: {detail}")
        raise MCPAuthenticationError("MCP authentication returned an empty response")

    async def _subscribe(self) -> None:
        await self._send_frame(self.protocol.subscribe_frame())

    async def _dispatch(self, parsed: ParsedMessage) -> None:
        callback = self.on_message
        try:
            # Prefer the explicit three-argument SPEC shape when the callback
            # can accept it.  Callable objects and decorated functions often
            # hide signatures; in that case try the SPEC shape first and only
            # fall back to the ParsedMessage shape on an argument mismatch.
            accepts_three = _accepts_positional(callback, 3)
            accepts_one = _accepts_positional(callback, 1)
            if accepts_three is True or accepts_one is None:
                result = callback(parsed.sender, parsed.message, parsed.raw)
            elif accepts_one is True:
                result = callback(parsed)
            else:
                result = callback(parsed.sender, parsed.message, parsed.raw)
        except TypeError:
            # Do not hide TypeErrors raised *inside* a callback whose
            # signature is known.  Only retry for genuinely ambiguous callables.
            if _accepts_positional(callback, 3) is not None or _accepts_positional(callback, 1) is not None:
                raise
            result = callback(parsed)
        if inspect.isawaitable(result):
            await result

    async def _receive_loop(self) -> None:
        while not self._stop_event.is_set():
            frame = await self._receive_frame()
            if self.protocol.is_event(frame):
                parsed = self.parser.parse(frame)
                if parsed is not None:
                    await self._dispatch(parsed)
                continue
            # Some wrappers omit ``type:event`` but retain the event name and
            # message fields.  The parser's marker requirement is configurable;
            # try it for such frames without treating auth/subscribe replies as
            # messages.
            if self.protocol.event_name_path is not None:
                parsed = self.parser.parse(frame)
                if parsed is not None:
                    await self._dispatch(parsed)

    async def _run_once(self) -> None:
        ws = await self._open()
        self._ws = ws
        self._connection_count += 1
        self._authenticated = False
        self._connected_event.set()
        try:
            await self._authenticate()
            await self._subscribe()
            await self._receive_loop()
        finally:
            self._authenticated = False
            self._connected_event.clear()
            self._ws = None
            close = getattr(ws, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def connect(self) -> None:
        """Run the client until disconnected, reconnecting as configured.

        Calling ``connect`` a second time while the first call is active is
        harmless: it waits for the existing lifecycle task when one exists
        rather than opening two sockets.
        """

        if self._task is not None and self._task is not asyncio.current_task():
            await self._task
            return
        if self._stop_event.is_set() and self._disconnect_requested:
            # A client can be reused after disconnect().
            self._stop_event = asyncio.Event()
            self._disconnect_requested = False
        self._task = asyncio.current_task()
        delay = self.reconnect_initial_delay
        first_attempt = True
        self._last_error = None
        try:
            while not self._stop_event.is_set():
                try:
                    await self._run_once()
                    if self._stop_event.is_set():
                        break
                    # A clean peer close is still an interruption from the
                    # client's perspective, so reconnect unless explicitly
                    # stopped.
                    error: BaseException = MCPConnectionError("MCP server closed the connection")
                    self._last_error = error
                except asyncio.CancelledError:
                    raise
                except (MCPAuthenticationError, ProtocolDecodeError) as exc:
                    self._last_error = exc
                    if first_attempt and not self.reconnect:
                        raise
                    # Authentication and malformed server frames are normally
                    # recoverable after a server restart/config reload.
                except (ConnectionClosed, WebSocketException, OSError, EOFError, MCPClientError) as exc:
                    self._last_error = exc
                    if first_attempt and not self.reconnect:
                        # A normal peer close after the callback has received
                        # the event is a clean test/server shutdown, not an
                        # authentication or transport failure.
                        if isinstance(exc, ConnectionClosed) and getattr(exc, "code", None) == 1000:
                            break
                        raise MCPConnectionError(str(exc)) from exc
                except Exception as exc:
                    self._last_error = exc
                    if first_attempt and not self.reconnect:
                        raise
                    self.logger.exception("unexpected MCP client error")

                first_attempt = False
                if not self.reconnect or self._stop_event.is_set():
                    break
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                delay = min(
                    self.reconnect_max_delay,
                    max(self.reconnect_initial_delay, delay * 2 or self.reconnect_initial_delay),
                )
        finally:
            self._connected_event.clear()
            self._authenticated = False
            self._task = None

    async def start(self) -> asyncio.Task[Any]:
        """Start :meth:`connect` in the background and return its task."""

        if self._task is not None:
            return self._task
        self._stop_event = asyncio.Event()
        self._disconnect_requested = False
        self._task = asyncio.create_task(self.connect())
        return self._task

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until the socket has been opened (authentication may follow)."""

        waiter = self._connected_event.wait()
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout=timeout)

    async def disconnect(self) -> None:
        """Stop receiving, close the active socket, and cancel background work."""

        self._disconnect_requested = True
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            close = getattr(ws, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        task = self._task
        if task is not None and task is not asyncio.current_task():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._connected_event.clear()
        self._authenticated = False
        self._ws = None

    async def __aenter__(self) -> "MCPClient":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()


def _accepts_positional(callback: MessageCallback, count: int) -> bool | None:
    """Best-effort signature check; ``None`` means unknown."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return None
    parameters = list(signature.parameters.values())
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return True
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    required = [parameter for parameter in positional if parameter.default is inspect.Parameter.empty]
    if len(required) > count:
        return False
    return len(positional) >= count or len(required) <= count


__all__ = [
    "MCPAuthenticationError",
    "MCPClient",
    "MCPClientClosed",
    "MCPClientError",
    "MCPConnectionError",
]
