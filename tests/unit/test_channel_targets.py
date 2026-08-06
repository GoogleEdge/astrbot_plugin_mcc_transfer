import json
from pathlib import Path

from astrbot_adapter import build_umo, resolve_target
from config import AppConfig


def test_qq_official_uses_native_three_part_umo():
    assert build_umo(
        platform_name="qq_official",
        platform_instance="default",
        message_type="GroupMessage",
        group_id="group-openid-123",
    ) == "qq_official:GroupMessage:group-openid-123"


def test_aiocqhttp_uses_native_three_part_umo():
    assert resolve_target({"platform_name": "aiocqhttp", "group_id": "123456"}).umo == (
        "aiocqhttp:GroupMessage:123456"
    )


def test_custom_umo_template_can_include_instance():
    target = resolve_target(
        {
            "platform_name": "aiocqhttp",
            "platform_instance": "bot-1",
            "group_id": "123456",
            "umo_template": "{platform_name}:{platform_instance}:{message_type}:{group_id}",
        }
    )
    assert target.umo == "aiocqhttp:bot-1:GroupMessage:123456"


def test_app_config_defaults_to_qq_official():
    config = AppConfig.from_mapping({}, require_target=False)

    assert config.target.platform_name == "qq_official"
    assert config.target.umo_template == ""


def test_metadata_supports_both_channels():
    metadata = Path(__file__).parents[2] / "metadata.yaml"
    values = metadata.read_text(encoding="utf-8")

    assert "  - qq_official" in values
    assert "  - aiocqhttp" in values


def test_schema_defaults_to_qq_official():
    schema = json.loads((Path(__file__).parents[2] / "_conf_schema.json").read_text(encoding="utf-8"))

    assert schema["target"]["items"]["platform_name"]["default"] == "qq_official"
    assert schema["target"]["items"]["umo_template"]["default"] == ""
