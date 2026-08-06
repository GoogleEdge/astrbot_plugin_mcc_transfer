from unittest.mock import AsyncMock

import pytest

from astrbot_adapter import AstrBotDeliveryError, AstrBotSender
from config import AppConfig, ConfigError
from main import MCCTransferPlugin


def test_native_config_can_defer_target_validation():
    config = AppConfig.from_mapping({}, require_target=False)

    assert config.target.group_id == ""
    with pytest.raises(ConfigError, match="target.group_id is required"):
        AppConfig.from_mapping({})


@pytest.mark.asyncio
async def test_sender_surfaces_explicit_astrbot_rejection():
    class Context:
        async def send_message(self, _umo, _chain):
            return False

    with pytest.raises(AstrBotDeliveryError, match="rejected proactive message"):
        await AstrBotSender(Context(), message_chain_factory=lambda: object()).send(
            "hello", "qqbot:GroupMessage:session"
        )


@pytest.mark.asyncio
async def test_plugin_stays_inactive_until_target_is_configured(tmp_path):
    plugin = MCCTransferPlugin(context=object(), config={})
    plugin.runtime.data_dir = tmp_path
    plugin.runtime._status_path = tmp_path / "status.json"
    plugin.runtime.start = AsyncMock()

    await plugin.initialize()

    plugin.runtime.start.assert_not_awaited()
    assert (tmp_path / "status.json").exists()


@pytest.mark.asyncio
async def test_plugin_starts_after_target_is_configured():
    plugin = MCCTransferPlugin(context=object(), config={"target": {"group_id": "123456"}})
    plugin.runtime.start = AsyncMock()

    await plugin.initialize()

    plugin.runtime.start.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_loaded_hook_and_initialize_are_idempotent():
    plugin = MCCTransferPlugin(context=object(), config={"target": {"group_id": "123456"}})
    calls = []

    async def guarded_start():
        if not plugin.runtime._started:
            plugin.runtime._started = True
            calls.append(True)

    plugin.runtime.start = guarded_start

    await plugin.initialize()
    await plugin.on_astrbot_loaded()

    assert len(calls) == 1
