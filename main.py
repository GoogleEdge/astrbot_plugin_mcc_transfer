"""AstrBot entry point for the MCC chat forwarder."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    from .runtime import PluginRuntime
except ImportError:  # pragma: no cover - standalone fallback
    from runtime import PluginRuntime

LOGGER = logging.getLogger(__name__)

try:  # AstrBot is available when the plugin is loaded by the host.
    from astrbot.api import logger as astrbot_logger  # type: ignore
    from astrbot.api.event import AstrMessageEvent, filter  # type: ignore
    from astrbot.api.star import Context, Star, register  # type: ignore
    from astrbot.core.config.astrbot_config import AstrBotConfig  # type: ignore
except ImportError:  # pragma: no cover - exercised by standalone tests
    astrbot_logger = LOGGER

    class Context:  # type: ignore[no-redef]
        pass

    class AstrMessageEvent:  # type: ignore[no-redef]
        unified_msg_origin: str = ""

    class AstrBotConfig(dict):  # type: ignore[no-redef]
        pass

    class Star:  # type: ignore[no-redef]
        def __init__(self, context: Any) -> None:
            self.context = context

    class _Filter:
        @staticmethod
        def on_astrbot_loaded():
            def decorator(func):
                return func

            return decorator

    filter = _Filter()  # type: ignore[assignment]

    def register(*args: Any, **kwargs: Any):  # type: ignore[no-redef]
        def decorator(cls):
            return cls

        return decorator

try:
    from astrbot.core.star.context import get_astrbot_data_path  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback
    def get_astrbot_data_path() -> str:
        return str(Path("data"))

try:
    from .config import AppConfig
except ImportError:  # pragma: no cover - standalone fallback
    try:
        from config import AppConfig
    except ImportError:  # pragma: no cover - partial install fallback
        AppConfig = None  # type: ignore[assignment,misc]


@register("astrbot_plugin_mcc_transfer", "MCC Transfer", "Deterministic MCC chat forwarding", "1.0.0")
class MCCTransferPlugin(Star):
    """Start and stop the forwarding runtime with AstrBot's lifecycle."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or AstrBotConfig()
        normalized = self.config
        if AppConfig is not None and hasattr(AppConfig, "from_mapping"):
            # AstrBot instantiates the plugin before the user can fill in its
            # WebUI configuration. Keep structural validation here, then let
            # runtime startup enforce that a forwarding target is configured.
            normalized = AppConfig.from_mapping(self.config, require_target=False)
        data_root = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_mcc_transfer"
        self.runtime = PluginRuntime(normalized, context=context, data_dir=data_root)

    @staticmethod
    def _has_configured_target(config: Any) -> bool:
        target = getattr(config, "target", None)
        if target is None and hasattr(config, "get"):
            target = config.get("target", config)
        if target is None:
            return False
        get = target.get if hasattr(target, "get") else lambda key, default=None: getattr(target, key, default)
        return bool(
            str(get("group_id", "") or "").strip()
            or str(get("umo_override", get("umo", "")) or "").strip()
        )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self, event: AstrMessageEvent | None = None) -> None:
        if not self._has_configured_target(self.runtime.config):
            astrbot_logger.warning(
                "MCC transfer is inactive: configure target.group_id, target.umo_override, or target.umo "
                "in the plugin settings, then reload the plugin"
            )
            return
        await self.runtime.start()

    async def terminate(self) -> None:
        await self.runtime.stop()
