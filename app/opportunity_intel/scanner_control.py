"""Authoritative Market Opportunity scanner arming control.

Live Mode ON for any configured administrator arms the scanner worker.
The process env flag remains a legacy/bootstrap OR. Display and job ticks
must use these helpers so Live Mode and Scanner Status cannot disagree.
"""

from __future__ import annotations

from app.config.settings import Settings
from app.database import opportunity_storage
from app.opportunity_intel.models import LiveMode
from app.telegram.auth import is_admin


def any_admin_live_mode_on(settings: Settings) -> bool:
    """True when at least one ADMIN_IDS recipient has Live Mode ON (STRICT)."""
    admin_ids = tuple(settings.admin_ids or ())
    if not admin_ids:
        return False
    try:
        for telegram_id in admin_ids:
            subscription = opportunity_storage.load_subscription(
                settings, int(telegram_id)
            )
            if subscription is None:
                continue
            if subscription.settings.live_mode == LiveMode.STRICT:
                return True
    except Exception:
        # Schema may not exist yet during early app bootstrap; fail closed.
        return False
    return False


def scanner_armed(settings: Settings) -> bool:
    """Single source of truth for whether Polymarket opportunity scanning may run."""
    if any_admin_live_mode_on(settings):
        return True
    return bool(settings.opportunity_scanner_enabled)


def auto_alerts_armed(settings: Settings) -> bool:
    """Whether the delivery worker may send automatic alerts this process."""
    if any_admin_live_mode_on(settings):
        return True
    return bool(settings.opportunity_auto_alerts_enabled)


def viewer_is_admin(settings: Settings, telegram_id: int | None) -> bool:
    return is_admin(settings, telegram_id)
