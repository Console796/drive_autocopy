from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base) / "synctool"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    log_dir = state_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "synctool.log"

    logger = logging.getLogger("synctool")
    logger.setLevel(level)
    if not logger.handlers:
        handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger
