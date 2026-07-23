"""Wallet watchlist service."""

from __future__ import annotations

from app.config.settings import Settings
from app.copy_intel.data_client import normalize_wallet
from app.copy_intel.freshness import begin_subscription
from app.database import copy_storage


def add_wallet(
    settings: Settings,
    address: str,
    *,
    label: str | None = None,
    added_by: int | None = None,
) -> str:
    wallet = normalize_wallet(address)
    prior = copy_storage.get_watchlist_entry(settings, wallet)
    copy_storage.add_watchlist_wallet(settings, wallet, label=label, added_by=added_by)
    copy_storage.write_audit_log(
        settings,
        actor=str(added_by or "system"),
        action="watchlist_add",
        payload={"wallet": wallet, "label": label},
    )
    # New wallet, or re-add of a previously disabled wallet: start baseline.
    if prior is None or not prior.enabled:
        begin_subscription(settings, wallet)
    return wallet


def remove_wallet(settings: Settings, address: str, *, actor: str = "system") -> bool:
    wallet = normalize_wallet(address)
    removed = copy_storage.remove_watchlist_wallet(settings, wallet)
    if removed:
        _mark_monitoring_disabled(settings, wallet)
        copy_storage.write_audit_log(
            settings,
            actor=actor,
            action="watchlist_remove",
            payload={"wallet": wallet},
        )
    return removed


def toggle_wallet(settings: Settings, address: str, *, actor: str = "system") -> bool | None:
    wallet = normalize_wallet(address)
    enabled = copy_storage.toggle_watchlist_wallet(settings, wallet)
    if enabled is not None:
        copy_storage.write_audit_log(
            settings,
            actor=actor,
            action="watchlist_toggle",
            payload={"wallet": wallet, "enabled": enabled},
        )
        if enabled:
            begin_subscription(settings, wallet)
        else:
            _mark_monitoring_disabled(settings, wallet)
    return enabled


def set_monitoring(
    settings: Settings,
    address: str,
    *,
    enabled: bool,
    actor: str = "system",
) -> str:
    """
    Idempotent pause/resume.
    Returns: 'enabled' | 'disabled' | 'already_enabled' | 'already_disabled' | 'missing'
    Resume always starts a fresh baseline (never replays history).
    """
    wallet = normalize_wallet(address)
    entry = copy_storage.get_watchlist_entry(settings, wallet)
    if entry is None:
        return "missing"
    if enabled and entry.enabled:
        return "already_enabled"
    if (not enabled) and (not entry.enabled):
        return "already_disabled"

    copy_storage.set_watchlist_enabled(settings, wallet, enabled=enabled)
    copy_storage.write_audit_log(
        settings,
        actor=actor,
        action="watchlist_set_monitoring",
        payload={"wallet": wallet, "enabled": enabled},
    )
    if enabled:
        begin_subscription(settings, wallet)
        return "enabled"
    _mark_monitoring_disabled(settings, wallet)
    return "disabled"


def can_manage_entry(
    entry: copy_storage.WatchlistEntry | None,
    user_id: int | None,
    *,
    is_admin: bool,
) -> bool:
    """Row operations require admin AND exact added_by ownership (V2.2.2)."""
    if entry is None or user_id is None:
        return False
    if not is_admin:
        return False
    return entry.added_by == user_id


def list_entries_for_user(
    settings: Settings,
    user_id: int | None,
    *,
    is_admin: bool,
) -> list[copy_storage.WatchlistEntry]:
    """User-facing watchlist is always owner-scoped; admin flag is access gate only."""
    if user_id is None or not is_admin:
        return []
    return copy_storage.list_watchlist_for_owner(settings, int(user_id))


def list_entries(settings: Settings) -> list[copy_storage.WatchlistEntry]:
    """Internal/global listing (health/maintenance). Prefer list_entries_for_user in handlers."""
    return copy_storage.list_watchlist(settings)


def list_enabled(settings: Settings) -> list[str]:
    return copy_storage.list_enabled_wallets(settings)


def profile_url(wallet: str) -> str:
    from app.telegram.copy_intel.formatting import build_validated_trader_link

    link = build_validated_trader_link(wallet)
    if link is None:
        raise ValueError("invalid wallet for profile link")
    return link


def _mark_monitoring_disabled(settings: Settings, wallet: str) -> None:
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    if state is not None:
        copy_storage.update_wallet_monitor_fields(
            settings,
            wallet,
            monitoring_enabled=False,
        )
