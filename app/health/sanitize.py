"""Sanitize health output — never expose secrets or unsafe paths."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(token|api[_-]?key|secret|password|credential|authorization|private)",
    re.IGNORECASE,
)
_URL_CRED_RE = re.compile(r"://([^:/@]+):([^@]+)@")


def hash_user_id(user_id: int) -> str:
    return hashlib.sha256(f"tg:{user_id}".encode("utf-8")).hexdigest()[:16]


def redact_string(value: str) -> str:
    text = _URL_CRED_RE.sub("://***:***@", value)
    if len(text) > 240:
        return text[:240] + "…"
    return text


def sanitize_path(path: str | Path | None) -> str:
    if not path:
        return "(unset)"
    p = Path(str(path))
    return p.name or str(p)


def sanitize_mapping(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _SECRET_KEY_RE.search(str(key)):
            out[str(key)] = "***"
            continue
        if isinstance(value, dict):
            out[str(key)] = sanitize_mapping(value)
        elif isinstance(value, str):
            out[str(key)] = redact_string(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[str(key)] = value
        else:
            out[str(key)] = redact_string(str(value))
    return out


def sanitize_config_summary(settings) -> dict[str, Any]:
    return {
        "copy_intel_enabled": settings.copy_intel_enabled,
        "copy_intel_read_only": settings.copy_intel_read_only,
        "health_monitor_enabled": settings.health_monitor_enabled,
        "health_alerts_enabled": settings.health_alerts_enabled,
        "admin_count": len(settings.admin_ids),
        "database": sanitize_path(settings.database_path),
        "copy_poll_interval_seconds": settings.copy_poll_interval_seconds,
        "health_check_interval_seconds": settings.health_check_interval_seconds,
        "forecast_min_providers": settings.forecast_min_providers,
        "telegram_token": "***",
        "anthropic_api_key": "***",
    }
