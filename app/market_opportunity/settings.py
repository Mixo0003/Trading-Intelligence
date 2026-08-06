"""Feature-flag helpers for Market Opportunity Intelligence."""

from __future__ import annotations

from app.config.settings import Settings
from app.market_opportunity.paper import assert_paper_only


def module_enabled(settings: Settings) -> bool:
    return bool(settings.market_opportunity_enabled)


def auto_alerts_enabled(settings: Settings) -> bool:
    return bool(settings.market_opportunity_auto_alerts_enabled)


def assert_safe_defaults_for_v41(settings: Settings) -> None:
    """V4.1 runtime posture: paper-only, no auto alerts."""
    assert_paper_only(settings)
