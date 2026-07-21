"""Bounded application log rotation (Windows-compatible)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config.settings import Settings
from app.storage.paths import reject_reason_for_configured_root, resolve_approved_root

logger = logging.getLogger(__name__)

_FILE_HANDLER_NAME = "ti_rotating_file"


def active_log_file(settings: Settings) -> Path | None:
    root = resolve_approved_root(settings.storage_logs_dir)
    if root is None:
        return None
    name = (settings.storage_active_log_name or "").strip()
    if not name or ".." in Path(name).parts or Path(name).is_absolute():
        return None
    return root / name


def ensure_rotating_file_logging(settings: Settings) -> bool:
    """
    Attach a RotatingFileHandler when file logging is explicitly enabled and
    the logs directory is a valid approved root. Never deletes the active log.
    """
    if not settings.storage_file_logging_enabled:
        return False
    reason = reject_reason_for_configured_root(settings.storage_logs_dir)
    if reason is not None:
        return False
    root = resolve_approved_root(settings.storage_logs_dir)
    if root is None:
        return False
    log_path = active_log_file(settings)
    if log_path is None:
        return False

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Unable to create logs directory for file logging")
        return False

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if getattr(handler, "name", None) == _FILE_HANDLER_NAME:
            return True

    handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=max(1024, int(settings.storage_log_max_bytes)),
        backupCount=max(1, int(settings.storage_log_backup_count)),
        encoding="utf-8",
        delay=True,
    )
    handler.set_name(_FILE_HANDLER_NAME)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    )
    root_logger.addHandler(handler)
    return True
