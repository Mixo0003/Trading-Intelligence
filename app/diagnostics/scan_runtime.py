"""Secret-safe runtime diagnostics for scan wiring verification."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOGGED = False
_STARTUP_TS = datetime.now(timezone.utc).isoformat()


def _git_commit() -> str:
    try:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _module_path(module_name: str) -> str:
    try:
        module = __import__(module_name, fromlist=["__file__"])
        return str(getattr(module, "__file__", "unknown"))
    except Exception as exc:
        return f"import_error:{exc}"


def log_scan_runtime_diagnostics() -> None:
    global _LOGGED
    if _LOGGED:
        return
    _LOGGED = True

    repo_root = str(Path(__file__).resolve().parents[2])
    payload = {
        "python_executable": sys.executable,
        "repository_root": repo_root,
        "process_id": os.getpid(),
        "bot_startup_timestamp_utc": _STARTUP_TS,
        "git_commit": _git_commit(),
        "modules": {
            "event_analysis": _module_path("app.signals.event_analysis"),
            "sibling_outcomes": _module_path("app.polymarket.sibling_outcomes"),
            "bucket_probability": _module_path("app.signals.bucket_probability"),
            "market_type": _module_path("app.polymarket.market_type"),
            "report_formatter": _module_path("app.telegram.formatting"),
            "telegram_scan_handler": _module_path("app.telegram.handlers"),
            "scan_service": _module_path("app.signals.scan_service"),
            "legacy_market_analysis": _module_path("app.signals.market_analysis"),
            "event_expansion": _module_path("app.polymarket.event_expansion"),
        },
    }
    logger.info("SCAN_RUNTIME_DIAG %s", payload)
