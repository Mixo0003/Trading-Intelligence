"""Top Opportunities loader for V4.3 crypto scanner."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.opportunity.filters import qualify_for_user
from app.crypto.opportunity.models import PaperNetOpportunity, ScanReport
from app.crypto.opportunity.scanner import scan_crypto_opportunities
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    balanced_crypto_settings,
)
from app.database import crypto_opportunity_storage as storage

TOP_LIMIT = 3


def load_top_crypto_opportunities(
    settings: Settings,
    *,
    telegram_id: int,
    user_settings: CryptoOpportunitySettings | None = None,
) -> ScanReport:
    profile = user_settings or storage.get_or_default_settings(settings, telegram_id)
    report = scan_crypto_opportunities(settings, user_settings=profile)
    if not report.enabled:
        return report

    qualified: list[PaperNetOpportunity] = []
    for opportunity in report.opportunities:
        last_sent = storage.last_sent_at_for_key(
            settings,
            telegram_id=telegram_id,
            opportunity_key=opportunity.opportunity_key,
        )
        ok, _reason = qualify_for_user(
            opportunity,
            profile,
            last_sent_at=last_sent,
            require_delivery_enabled=False,
            apply_cooldown=False,  # on-demand Top ignores cooldown
        )
        if ok:
            qualified.append(opportunity)
        if len(qualified) >= TOP_LIMIT:
            break
    return ScanReport(
        enabled=True,
        opportunities=tuple(qualified),
        rejects=report.rejects,
        diagnostics=report.diagnostics,
    )


def ensure_default_profile(settings: Settings, telegram_id: int) -> CryptoOpportunitySettings:
    return storage.ensure_profile(settings, telegram_id, balanced_crypto_settings())
