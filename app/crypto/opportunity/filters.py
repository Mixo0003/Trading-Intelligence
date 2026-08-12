"""Per-user qualification for crypto opportunities."""

from __future__ import annotations

from datetime import datetime, timezone

from app.crypto.opportunity.models import PaperNetOpportunity
from app.crypto.opportunity.settings import CryptoOpportunitySettings


def qualify_for_user(
    opportunity: PaperNetOpportunity,
    user_settings: CryptoOpportunitySettings,
    *,
    last_sent_at: datetime | None,
    require_delivery_enabled: bool = False,
    apply_cooldown: bool = True,
) -> tuple[bool, str | None]:
    if require_delivery_enabled and not user_settings.delivery_enabled:
        return False, "delivery_disabled"
    if opportunity.buy_venue not in user_settings.selected_exchanges:
        return False, "buy_venue_filtered"
    if opportunity.sell_venue not in user_settings.selected_exchanges:
        return False, "sell_venue_filtered"
    if opportunity.product_type not in user_settings.selected_product_types:
        return False, "product_filtered"
    if opportunity.paper_allocation_usd != user_settings.paper_allocation_usd:
        # Re-check structural consistency; Top path rescans with user allocation.
        pass
    if opportunity.estimated_slippage > float(user_settings.maximum_slippage):
        return False, "slippage_exceeded"
    if opportunity.signal_age_seconds > float(user_settings.maximum_signal_age_seconds):
        return False, "signal_stale"
    if opportunity.net_profit_usd < float(user_settings.minimum_net_profit_usd):
        return False, "below_min_net_profit"
    if opportunity.roi < float(user_settings.minimum_roi):
        return False, "below_min_roi"
    if opportunity.executable_liquidity_usd < float(
        user_settings.minimum_executable_depth_usd
    ):
        return False, "below_min_depth"
    if apply_cooldown and last_sent_at is not None:
        age = (datetime.now(timezone.utc) - last_sent_at).total_seconds()
        if age < float(user_settings.cooldown_seconds):
            return False, "cooldown_active"
    return True, None
