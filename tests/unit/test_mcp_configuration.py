from config import AppConfig, ConfigError


def test_http_mcp_defaults_match_current_mcc_endpoint():
    config = AppConfig.from_mapping({"target": {"group_id": "session"}})

    assert config.mcp.transport == "http"
    assert config.mcp.url == "http://127.0.0.1:33333/mcp"
    assert config.mcp.chat_tool == "mcc_chat_history"
    assert config.sender.mode == "native"


def test_sender_openapi_settings_are_parsed_without_storing_key():
    config = AppConfig.from_mapping(
        {
            "sender": {
                "mode": "openapi",
                "endpoint": "http://127.0.0.1:6185/api/v1/im/message",
                "api_key_env": "MY_OPENAPI_KEY",
                "auth_header": "x-api-key",
            },
            "target": {"umo_override": "qqbot:GroupMessage:session"},
        }
    )

    assert config.sender.mode == "openapi"
    assert config.sender.api_key_env == "MY_OPENAPI_KEY"
    assert "secret" not in repr(config)
    assert "api_key" not in config.to_dict()["sender"]


def test_sender_endpoint_rejects_query_credentials():
    with __import__("pytest").raises(ConfigError, match="credentials"):
        AppConfig.from_mapping(
            {
                "sender": {
                    "mode": "openapi",
                    "endpoint": "http://127.0.0.1:6185/api/v1/im/message?key=secret",
                },
                "target": {"group_id": "session"},
            }
        )


def test_required_auth_rejects_blank_password():
    try:
        AppConfig.from_mapping(
            {
                "mcp": {"transport": "websocket", "auth_mode": "required", "password": ""},
                "target": {"group_id": "session"},
            }
        )
    except ConfigError as exc:
        assert "password" in str(exc)
    else:
        raise AssertionError("blank required password was accepted")


def test_auto_auth_allows_blank_password_for_unauthenticated_profile():
    config = AppConfig.from_mapping(
        {
            "mcp": {"transport": "websocket", "auth_mode": "auto", "password": ""},
            "target": {"group_id": "session"},
        }
    )

    assert config.mcp.auth_mode == "auto"
