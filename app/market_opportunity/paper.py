"""Paper-only safety gates for Market Opportunity Intelligence."""

from __future__ import annotations

from app.config.settings import Settings


class MarketOpportunitySafetyError(RuntimeError):
    """Raised when paper-only / read-only invariants are violated."""


def assert_paper_only(settings: Settings) -> None:
    """Require read-only + paper posture. Never places real orders."""
    if not settings.market_opportunity_read_only:
        raise MarketOpportunitySafetyError(
            "MARKET_OPPORTUNITY_READ_ONLY must remain true"
        )
    if settings.market_opportunity_auto_alerts_enabled:
        raise MarketOpportunitySafetyError(
            "MARKET_OPPORTUNITY_AUTO_ALERTS_ENABLED must remain false in V4.1"
        )
