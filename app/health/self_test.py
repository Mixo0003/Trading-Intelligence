"""Admin-triggered health self-test (read-only probes)."""

from __future__ import annotations

from app.config.settings import Settings
from app.health.monitor import run_health_check
from app.health.models import OverallHealth
from app.health.sanitize import sanitize_config_summary, sanitize_mapping


def run_self_test(settings: Settings, *, bot_data: dict | None = None) -> dict:
    """Run deterministic probes; never mutates .env or restarts processes."""
    overall: OverallHealth = run_health_check(settings, bot_data=bot_data)
    return {
        "overall": overall.status,
        "summary": overall.summary,
        "checked_at": overall.checked_at,
        "components": [
            {
                "component": c.component,
                "status": c.status,
                "detail": c.detail,
                "metrics": sanitize_mapping(c.metrics),
            }
            for c in overall.components
        ],
        "config": sanitize_config_summary(settings),
        "ai_explainer": False,
    }
