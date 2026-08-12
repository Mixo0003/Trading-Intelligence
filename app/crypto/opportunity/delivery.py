"""Delivery wave selection for crypto opportunities (no Telegram send)."""

from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings
from app.crypto.opportunity.filters import qualify_for_user
from app.crypto.opportunity.formatting import format_crypto_opportunity_card
from app.crypto.opportunity.models import PaperNetOpportunity
from app.crypto.opportunity.settings import CryptoOpportunitySettings
from app.database import crypto_opportunity_storage as storage


@dataclass(frozen=True)
class CryptoWaveResult:
    opportunity_uid: str
    pending_count: int
    suppressed_count: int
    ignored_duplicates: int


def select_crypto_delivery_wave(
    settings: Settings,
    opportunity: PaperNetOpportunity,
    *,
    recipients: list[tuple[int, CryptoOpportunitySettings]],
) -> CryptoWaveResult:
    pending = 0
    suppressed = 0
    ignored = 0
    for telegram_id, user_settings in recipients:
        last_sent = storage.last_sent_at_for_key(
            settings,
            telegram_id=telegram_id,
            opportunity_key=opportunity.opportunity_key,
        )
        ok, reason = qualify_for_user(
            opportunity,
            user_settings,
            last_sent_at=last_sent,
            require_delivery_enabled=True,
            apply_cooldown=True,
        )
        payload = format_crypto_opportunity_card(opportunity)
        if not ok:
            storage.insert_delivery(
                settings,
                telegram_id=telegram_id,
                opportunity_uid=opportunity.opportunity_uid,
                opportunity_key=opportunity.opportunity_key,
                payload_text=payload,
                status="SUPPRESSED",
            )
            suppressed += 1
            continue
        inserted = storage.insert_delivery(
            settings,
            telegram_id=telegram_id,
            opportunity_uid=opportunity.opportunity_uid,
            opportunity_key=opportunity.opportunity_key,
            payload_text=payload,
            status="PENDING",
        )
        if inserted:
            pending += 1
        else:
            ignored += 1
    return CryptoWaveResult(
        opportunity_uid=opportunity.opportunity_uid,
        pending_count=pending,
        suppressed_count=suppressed,
        ignored_duplicates=ignored,
    )


def recheck_delivery_authorization(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_key: str,
) -> tuple[bool, str | None]:
    """Re-check user settings immediately before send."""
    if not getattr(settings, "crypto_opportunity_delivery_enabled", False):
        return False, "global_delivery_disabled"
    sub = storage.get_subscription(settings, telegram_id)
    if sub is None or not sub.settings.delivery_enabled:
        return False, "delivery_disabled"
    last_sent = storage.last_sent_at_for_key(
        settings, telegram_id=telegram_id, opportunity_key=opportunity_key
    )
    # Minimal structural check without a live opportunity object.
    if last_sent is not None:
        from datetime import datetime, timezone

        age = (datetime.now(timezone.utc) - last_sent).total_seconds()
        if age < float(sub.settings.cooldown_seconds):
            return False, "cooldown_active"
    return True, None
