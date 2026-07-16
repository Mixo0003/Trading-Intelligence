"""Wallet watchlist service."""

from __future__ import annotations

from app.config.settings import Settings
from app.copy_intel.data_client import normalize_wallet
from app.database import copy_storage


def add_wallet(
    settings: Settings,
    address: str,
    *,
    label: str | None = None,
    added_by: int | None = None,
) -> str:
    wallet = normalize_wallet(address)
    copy_storage.add_watchlist_wallet(settings, wallet, label=label, added_by=added_by)
    copy_storage.write_audit_log(
        settings,
        actor=str(added_by or "system"),
        action="watchlist_add",
        payload={"wallet": wallet, "label": label},
    )
    return wallet


def remove_wallet(settings: Settings, address: str, *, actor: str = "system") -> bool:
    wallet = normalize_wallet(address)
    removed = copy_storage.remove_watchlist_wallet(settings, wallet)
    if removed:
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
    return enabled


def list_entries(settings: Settings) -> list[copy_storage.WatchlistEntry]:
    return copy_storage.list_watchlist(settings)


def list_enabled(settings: Settings) -> list[str]:
    return copy_storage.list_enabled_wallets(settings)
