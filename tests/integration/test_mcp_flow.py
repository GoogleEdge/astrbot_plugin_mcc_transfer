import asyncio

import pytest

from mcp_client import MCPClient
from mock_mcp_server import MockMCPServer


@pytest.mark.asyncio
async def test_mcp_mock_server_dispatches_event(unused_tcp_port):
    received = []

    async def callback(sender, message, raw):
        received.append((sender, message, raw))

    async with MockMCPServer(
        host="127.0.0.1",
        port=unused_tcp_port,
        password="secret",
        events=[
            {
                "type": "event",
                "event": "PlayerMessage",
                "player": "Alex",
                "message": "hello",
            }
        ],
    ):
        client = MCPClient(
            "127.0.0.1",
            unused_tcp_port,
            "secret",
            callback,
            reconnect=False,
        )
        task = asyncio.create_task(client.connect())
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        await client.disconnect()
        task.cancel()
        await task

    assert received
    assert received[0][0:2] == ("Alex", "hello")
