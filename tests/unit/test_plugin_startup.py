from unittest.mock import AsyncMock

import pytest

from config import AppConfig, ConfigError
from main import MCCTransferPlugin


def test_native_config_can_defer_target_validation():
    config = AppConfig.from_mapping({}, require_target=False)

    assert config.target.group_id == ""
    with pytest.raises(ConfigError, match="target.group_id is required"):
        AppConfig.from_mapping({})


@pytest.mark.asyncio
async def test_plugin_stays_inactive_until_target_is_configured():
    plugin = MCCTransferPlugin(context=object(), config={})
    plugin.runtime.start = AsyncMock()

    await plugin.on_astrbot_loaded()

    plugin.runtime.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_plugin_starts_after_target_is_configured():
    plugin = MCCTransferPlugin(context=object(), config={"target": {"group_id": "123456"}})
    plugin.runtime.start = AsyncMock()

    await plugin.on_astrbot_loaded()

    plugin.runtime.start.assert_awaited_once_with()
