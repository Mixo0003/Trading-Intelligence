"""Health probe helpers for Market Opportunity Intelligence."""

from __future__ import annotations

from typing import Any

from app.config.settings import Settings
from app.market_opportunity.detectors.registry import enabled_detectors
from app.market_opportunity.paper import MarketOpportunitySafetyError, assert_paper_only


def evaluate_moi_health(settings: Settings) -> dict[str, Any]:
    """
    Return a probe snapshot dict.
    Disabled is not a failure. Safety violations are hard failures.
    """
    metrics: dict[str, Any] = {
        "enabled": settings.market_opportunity_enabled,
        "auto_alerts": settings.market_opportunity_auto_alerts_enabled,
        "read_only": settings.market_opportunity_read_only,
        "enabled_detectors": list(enabled_detectors()),
    }
    if not settings.market_opportunity_enabled:
        return {
            "state": "DISABLED",
            "detail": "Market Opportunity Intelligence disabled",
            "probe_ok": True,
            "hard_down": False,
            "metrics": metrics,
        }
    try:
        assert_paper_only(settings)
    except MarketOpportunitySafetyError as exc:
        return {
            "state": "DOWN",
            "detail": str(exc),
            "probe_ok": False,
            "hard_down": True,
            "metrics": metrics,
        }
    return {
        "state": "HEALTHY",
        "detail": "Market Opportunity Intelligence idle (no live detectors)",
        "probe_ok": True,
        "hard_down": False,
        "metrics": metrics,
    }
