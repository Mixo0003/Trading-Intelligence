"""Copy-intel user settings profiles (Conservative / Balanced / Exploratory)."""

from __future__ import annotations

from app.copy_intel.models import CopyUserSettings, SettingsProfileName
from app.database import copy_storage

PROFILE_DEFAULTS: dict[SettingsProfileName, dict] = {
    "Conservative": {
        "min_resolved_markets": 40,
        "min_active_days": 60,
        "max_drawdown_pct": 0.25,
        "max_single_win_contribution": 0.25,
        "max_category_concentration": 0.35,
        "min_liquidity_usd": 1000.0,
        "poll_interval_seconds": 180,
        "alert_decisions": "FOLLOW_ONLY",
    },
    "Balanced": {
        "min_resolved_markets": 20,
        "min_active_days": 30,
        "max_drawdown_pct": 0.40,
        "max_single_win_contribution": 0.40,
        "max_category_concentration": 0.45,
        "min_liquidity_usd": 500.0,
        "poll_interval_seconds": 120,
        "alert_decisions": "FOLLOW_AND_WATCH",
    },
    "Exploratory": {
        "min_resolved_markets": 10,
        "min_active_days": 14,
        "max_drawdown_pct": 0.60,
        "max_single_win_contribution": 0.55,
        "max_category_concentration": 0.60,
        "min_liquidity_usd": 200.0,
        "poll_interval_seconds": 90,
        "alert_decisions": "FOLLOW_AND_WATCH",
    },
}


def defaults_for_profile(profile: SettingsProfileName) -> dict:
    return dict(PROFILE_DEFAULTS[profile])


def build_settings(telegram_id: int, profile: SettingsProfileName, **overrides) -> CopyUserSettings:
    base = defaults_for_profile(profile)
    base.update(overrides)
    return CopyUserSettings(
        telegram_id=telegram_id,
        profile=profile,
        min_resolved_markets=int(base["min_resolved_markets"]),
        min_active_days=int(base["min_active_days"]),
        max_drawdown_pct=float(base["max_drawdown_pct"]),
        max_single_win_contribution=float(base["max_single_win_contribution"]),
        max_category_concentration=float(base["max_category_concentration"]),
        min_liquidity_usd=float(base["min_liquidity_usd"]),
        poll_interval_seconds=int(base["poll_interval_seconds"]),
        alert_decisions=base["alert_decisions"],  # type: ignore[arg-type]
    )


def get_user_settings(settings, telegram_id: int) -> CopyUserSettings:
    stored = copy_storage.load_copy_user_settings(settings, telegram_id)
    if stored is None:
        return build_settings(telegram_id, "Balanced")
    return CopyUserSettings(
        telegram_id=telegram_id,
        profile=stored["profile"],
        min_resolved_markets=int(stored["min_resolved_markets"]),
        min_active_days=int(stored["min_active_days"]),
        max_drawdown_pct=float(stored["max_drawdown_pct"]),
        max_single_win_contribution=float(stored["max_single_win_contribution"]),
        max_category_concentration=float(stored["max_category_concentration"]),
        min_liquidity_usd=float(stored["min_liquidity_usd"]),
        poll_interval_seconds=int(stored["poll_interval_seconds"]),
        alert_decisions=stored["alert_decisions"],
    )


def set_profile(settings, telegram_id: int, profile: SettingsProfileName) -> CopyUserSettings:
    user = build_settings(telegram_id, profile)
    save_user_settings(settings, user)
    return user


def save_user_settings(settings, user: CopyUserSettings) -> None:
    copy_storage.save_copy_user_settings(
        settings,
        telegram_id=user.telegram_id,
        payload={
            "profile": user.profile,
            "min_resolved_markets": user.min_resolved_markets,
            "min_active_days": user.min_active_days,
            "max_drawdown_pct": user.max_drawdown_pct,
            "max_single_win_contribution": user.max_single_win_contribution,
            "max_category_concentration": user.max_category_concentration,
            "min_liquidity_usd": user.min_liquidity_usd,
            "poll_interval_seconds": user.poll_interval_seconds,
            "alert_decisions": user.alert_decisions,
        },
    )


def update_setting(settings, telegram_id: int, key: str, value) -> CopyUserSettings:
    user = get_user_settings(settings, telegram_id)
    allowed = {
        "min_resolved_markets",
        "min_active_days",
        "max_drawdown_pct",
        "max_single_win_contribution",
        "max_category_concentration",
        "min_liquidity_usd",
        "poll_interval_seconds",
        "alert_decisions",
    }
    if key not in allowed:
        raise ValueError(f"Unknown setting: {key}")
    data = {
        "min_resolved_markets": user.min_resolved_markets,
        "min_active_days": user.min_active_days,
        "max_drawdown_pct": user.max_drawdown_pct,
        "max_single_win_contribution": user.max_single_win_contribution,
        "max_category_concentration": user.max_category_concentration,
        "min_liquidity_usd": user.min_liquidity_usd,
        "poll_interval_seconds": user.poll_interval_seconds,
        "alert_decisions": user.alert_decisions,
    }
    if key == "alert_decisions":
        if value not in ("FOLLOW_ONLY", "FOLLOW_AND_WATCH"):
            raise ValueError("alert_decisions must be FOLLOW_ONLY or FOLLOW_AND_WATCH")
        data[key] = value
    elif key in {"min_resolved_markets", "min_active_days", "poll_interval_seconds"}:
        data[key] = int(value)
    else:
        data[key] = float(value)
    updated = build_settings(telegram_id, user.profile, **data)
    save_user_settings(settings, updated)
    return updated
