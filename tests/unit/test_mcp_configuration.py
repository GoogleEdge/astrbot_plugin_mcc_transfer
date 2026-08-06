from config import AppConfig, ConfigError


def test_http_mcp_defaults_match_current_mcc_endpoint():
    config = AppConfig.from_mapping({"target": {"group_id": "session"}})

    assert config.mcp.transport == "http"
    assert config.mcp.url == "http://127.0.0.1:33333/mcp"
    assert config.mcp.chat_tool == "mcc_chat_history"


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
