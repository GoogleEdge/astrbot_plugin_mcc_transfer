import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from http_mcp_client import HTTPMCPClient


class _Handler(BaseHTTPRequestHandler):
    request_count = 0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).request_count += 1
        method = payload.get("method")
        if method == "initialize":
            result: Any = {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"protocolVersion": "2025-03-26"}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "test-session")
        elif method == "tools/call":
            text = json.dumps({"events": [{"id": 1, "player": "Alex", "message": "hello"}]})
            result = {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"content": [{"type": "text", "text": text}]}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        else:
            result = {"jsonrpc": "2.0", "id": payload.get("id"), "result": {}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        body = ("data: " + json.dumps(result) + "\n\n").encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.mark.asyncio
async def test_http_mcp_initialize_and_poll():
    _Handler.request_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    received = []
    try:
        client = HTTPMCPClient(
            f"http://127.0.0.1:{server.server_port}/mcp",
            poll_interval=0,
            on_message=lambda event: received.append(event),
        )
        await client.initialize()
        assert client.session_id == "test-session"
        assert await client.poll_once() == 1
        assert received[0]["message"] == "hello"
    finally:
        server.shutdown()
        thread.join(timeout=2)
