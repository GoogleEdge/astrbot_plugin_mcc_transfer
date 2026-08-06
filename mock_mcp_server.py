"""Minimal MCC MCP WebSocket mock for manual and integration testing.

Run with ``powershell -NoProfile -Command "python mock_mcp_server.py"`` and
connect the plugin using the matching host, port, and password.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any

import websockets

LOGGER = logging.getLogger(__name__)


class MockMCPServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 25575,
        password: str = "your_secure_password",
        events: Iterable[dict[str, Any]] | None = None,
        close_after_event: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.events = list(events or [])
        self.close_after_event = close_after_event
        self.server: Any = None
        self.connections = 0

    async def _handler(self, websocket: Any) -> None:
        self.connections += 1
        authenticated = False
        subscribed = False
        try:
            async for payload in websocket:
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({"status": "error", "message": "invalid JSON"}))
                    continue
                frame_type = frame.get("type")
                if frame_type == "auth":
                    if frame.get("password") != self.password:
                        await websocket.send(json.dumps({"status": "error", "message": "invalid password"}))
                        await websocket.close()
                        return
                    authenticated = True
                    await websocket.send(json.dumps({"status": "success"}))
                elif frame_type == "subscribe" and authenticated:
                    if frame.get("event") != "PlayerMessage":
                        await websocket.send(json.dumps({"status": "error", "message": "unsupported event"}))
                        continue
                    subscribed = True
                    await websocket.send(json.dumps({"status": "success", "event": "PlayerMessage"}))
                    for event in self.events:
                        if not subscribed:
                            break
                        await websocket.send(json.dumps(event, ensure_ascii=False))
                        if self.close_after_event:
                            await websocket.close()
                            return
        except websockets.ConnectionClosed:
            LOGGER.debug("Mock client disconnected")

    async def start(self) -> Any:
        self.server = await websockets.serve(self._handler, self.host, self.port)
        return self.server

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def __aenter__(self) -> "MockMCPServer":
        await self.start()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.stop()


async def _run(args: argparse.Namespace) -> None:
    events = [
        {
            "type": "event",
            "event": "PlayerMessage",
            "player": args.player,
            "message": args.message,
        }
    ]
    async with MockMCPServer(
        args.host,
        args.port,
        args.password,
        events=events,
        close_after_event=args.close_after_event,
    ):
        LOGGER.info("Mock MCP server listening on ws://%s:%s", args.host, args.port)
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25575)
    parser.add_argument("--password", default="your_secure_password")
    parser.add_argument("--player", default="brightmoon")
    parser.add_argument("--message", default="大家好")
    parser.add_argument("--close-after-event", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
