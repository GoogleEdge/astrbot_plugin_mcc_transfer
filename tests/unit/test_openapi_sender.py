import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astrbot_adapter import AstrBotOpenAPIError, AstrBotOpenAPISender


class _Handler(BaseHTTPRequestHandler):
    status = 204
    payload = None
    headers = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).payload = json.loads(self.rfile.read(length))
        type(self).headers = dict(self.headers)
        self.send_response(type(self).status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"not-json response body")

    def log_message(self, *_args):
        return


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_header", ["bearer", "x-api-key"])
async def test_openapi_sender_posts_exact_umo_and_auth(auth_header):
    _Handler.status = 204
    _Handler.payload = None
    _Handler.headers = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sender = AstrBotOpenAPISender(
            f"http://127.0.0.1:{server.server_port}/api/v1/im/message",
            api_key_env="TEST_OPENAPI_KEY",
            auth_header=auth_header,
            environ={"TEST_OPENAPI_KEY": "test-secret"},
        )
        await sender.send("hello 世界", "qqbot:GroupMessage:session-123")

        assert _Handler.payload == {
            "umo": "qqbot:GroupMessage:session-123",
            "message": "hello 世界",
        }
        if auth_header == "bearer":
            assert _Handler.headers["Authorization"] == "Bearer test-secret"
        else:
            assert _Handler.headers["X-Api-Key"] == "test-secret"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_openapi_sender_accepts_direct_configured_key():
    _Handler.status = 204
    _Handler.payload = None
    _Handler.headers = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sender = AstrBotOpenAPISender(
            f"http://127.0.0.1:{server.server_port}/api/v1/im/message",
            api_key_env="MISSING_OPENAPI_KEY",
            api_key="direct-secret",
            environ={},
        )
        await sender.send("hello", "qqbot:GroupMessage:session-123")
        assert _Handler.headers["Authorization"] == "Bearer direct-secret"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_openapi_sender_hides_remote_error_body():
    _Handler.status = 403
    _Handler.payload = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sender = AstrBotOpenAPISender(
            f"http://127.0.0.1:{server.server_port}/api/v1/im/message",
            api_key_env="TEST_OPENAPI_KEY",
            environ={"TEST_OPENAPI_KEY": "test-secret"},
        )
        with pytest.raises(AstrBotOpenAPIError) as caught:
            await sender.send("private message", "qqbot:GroupMessage:session-123")
        assert "403" in str(caught.value)
        assert "not-json" not in str(caught.value)
        assert "test-secret" not in str(caught.value)
        assert "private message" not in str(caught.value)
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_openapi_sender_rejects_credentialed_endpoint():
    with pytest.raises(ValueError, match="credentials"):
        AstrBotOpenAPISender(
            "http://user:password@127.0.0.1:6185/api/v1/im/message",
            api_key_env="TEST_OPENAPI_KEY",
            environ={"TEST_OPENAPI_KEY": "unused"},
        )


def test_openapi_sender_diagnostics_are_redacted():
    sender = AstrBotOpenAPISender(
        "http://127.0.0.1:6185/api/v1/im/message",
        api_key_env="TEST_OPENAPI_KEY",
        api_key="direct-secret",
        environ={"TEST_OPENAPI_KEY": "test-secret"},
    )

    diagnostics = sender.diagnostics()
    assert diagnostics == {
        "mode": "openapi",
        "endpoint": "http://127.0.0.1:6185/api/v1/im/message",
        "auth_header": "bearer",
        "key_configured": True,
    }
    assert "direct-secret" not in json.dumps(diagnostics)
    assert "test-secret" not in json.dumps(diagnostics)
