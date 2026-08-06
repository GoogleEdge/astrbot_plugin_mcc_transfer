"""Logging setup shared by the plugin and standalone CLI."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def configure_logging(config: Any) -> logging.Logger:
    """Configure a named logger from a config object or mapping."""

    get = config.get if hasattr(config, "get") else lambda key, default=None: getattr(config, key, default)
    name = str(get("name", "astrbot_plugin_mcc_transfer"))
    level_name = str(get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_file = get("log_file", None)
    if log_file:
        path = Path(str(log_file))
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=int(get("max_bytes", 10 * 1024 * 1024)),
            backupCount=int(get("backup_count", 5)),
            encoding="utf-8",
        )
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
