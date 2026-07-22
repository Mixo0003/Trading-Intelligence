"""Copy-intel user settings profiles (Conservative / Balanced / Exploratory)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.copy_intel.models import CopyUserSettings, SettingsProfileName
from app.copy_intel.settings_bounds import (
    EDITABLE_KEYS,
    collect_warnings,
    expected_effect,
    format_setting_value,
    parse_and_validate,
)
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
        "max_event_age_seconds": 300,
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
        # max_event_age_seconds filled from global settings at build/load time
        "max_event_age_seconds": 600,
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
        "max_event_age_seconds": 900,
    },
}

_SETTING_KEYS = (
    "min_resolved_markets",
    "min_active_days",
    "max_drawdown_pct",
    "max_single_win_contribution",
    "max_category_concentration",
    "min_liquidity_usd",
    "poll_interval_seconds",
    "alert_decisions",
    "max_event_age_seconds",
)


def defaults_for_profile(
    profile: SettingsProfileName,
    *,
    global_max_event_age_seconds: int | None = None,
) -> dict:
    base = dict(PROFILE_DEFAULTS[profile])
    if profile == "Balanced" and global_max_event_age_seconds is not None:
        base["max_event_age_seconds"] = int(global_max_event_age_seconds)
    return base


def build_settings(
    telegram_id: int,
    profile: SettingsProfileName,
    *,
    global_max_event_age_seconds: int | None = None,
    **overrides,
) -> CopyUserSettings:
    base = defaults_for_profile(
        profile,
        global_max_event_age_seconds=global_max_event_age_seconds,
    )
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
        max_event_age_seconds=int(base["max_event_age_seconds"]),
    )


def settings_to_payload(user: CopyUserSettings) -> dict[str, Any]:
    return {
        "profile": user.profile,
        "min_resolved_markets": user.min_resolved_markets,
        "min_active_days": user.min_active_days,
        "max_drawdown_pct": user.max_drawdown_pct,
        "max_single_win_contribution": user.max_single_win_contribution,
        "max_category_concentration": user.max_category_concentration,
        "min_liquidity_usd": user.min_liquidity_usd,
        "poll_interval_seconds": user.poll_interval_seconds,
        "alert_decisions": user.alert_decisions,
        "max_event_age_seconds": user.max_event_age_seconds,
    }


def settings_fingerprint(payload: dict[str, Any]) -> str:
    canonical = {k: payload.get(k) for k in ("profile", *_SETTING_KEYS)}
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_modified_from_profile(user: CopyUserSettings, *, global_max_event_age_seconds: int | None = None) -> bool:
    defaults = defaults_for_profile(
        user.profile,
        global_max_event_age_seconds=global_max_event_age_seconds,
    )
    current = settings_to_payload(user)
    for key in _SETTING_KEYS:
        if current[key] != defaults[key]:
            return True
    return False


def _coerce_stored(
    stored: dict,
    telegram_id: int,
    *,
    global_max_event_age_seconds: int,
) -> CopyUserSettings:
    profile = stored.get("profile") or "Balanced"
    if profile not in PROFILE_DEFAULTS:
        profile = "Balanced"
    defaults = defaults_for_profile(
        profile,  # type: ignore[arg-type]
        global_max_event_age_seconds=global_max_event_age_seconds,
    )
    merged = dict(defaults)
    for key in _SETTING_KEYS:
        if key in stored and stored[key] is not None:
            merged[key] = stored[key]
    # Migration: missing max_event_age_seconds → global env-backed default.
    if "max_event_age_seconds" not in stored or stored.get("max_event_age_seconds") is None:
        merged["max_event_age_seconds"] = int(global_max_event_age_seconds)
    return build_settings(
        telegram_id,
        profile,  # type: ignore[arg-type]
        global_max_event_age_seconds=global_max_event_age_seconds,
        **{k: merged[k] for k in _SETTING_KEYS},
    )


def get_user_settings(settings, telegram_id: int) -> CopyUserSettings:
    global_age = int(settings.copy_max_event_age_seconds)
    stored = copy_storage.load_copy_user_settings(settings, telegram_id)
    if stored is None:
        return build_settings(
            telegram_id,
            "Balanced",
            global_max_event_age_seconds=global_age,
        )
    return _coerce_stored(
        stored,
        telegram_id,
        global_max_event_age_seconds=global_age,
    )


def set_profile(
    settings,
    telegram_id: int,
    profile: SettingsProfileName,
    *,
    action: str = "copy_settings_save",
) -> CopyUserSettings:
    user = build_settings(
        telegram_id,
        profile,
        global_max_event_age_seconds=int(settings.copy_max_event_age_seconds),
    )
    save_user_settings(settings, user, changed_keys=list(_SETTING_KEYS), action=action)
    return user


def save_user_settings(
    settings,
    user: CopyUserSettings,
    *,
    changed_keys: list[str] | None = None,
    old_payload: dict[str, Any] | None = None,
    action: str = "copy_settings_save",
) -> None:
    payload = settings_to_payload(user)
    copy_storage.save_copy_user_settings(
        settings,
        telegram_id=user.telegram_id,
        payload=payload,
        audit_action=action,
        audit_extra={
            "changed_keys": changed_keys or [],
            "old": {k: (old_payload or {}).get(k) for k in (changed_keys or [])},
            "new": {k: payload.get(k) for k in (changed_keys or [])},
            "settings_fingerprint": settings_fingerprint(payload),
        },
    )


def preview_setting_change(
    settings,
    telegram_id: int,
    key: str,
    raw_value,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Validate a single-key change. Returns (ok, error, preview_dict)."""
    if key not in EDITABLE_KEYS:
        return False, f"Unknown setting: {key}", None
    user = get_user_settings(settings, telegram_id)
    result = parse_and_validate(key, raw_value)
    if not result.ok:
        return False, result.error, None
    old_payload = settings_to_payload(user)
    new_value = result.value
    warnings = collect_warnings(
        key,
        new_value,
        poll_interval_seconds=user.poll_interval_seconds,
        global_poll_interval_seconds=settings.copy_poll_interval_seconds,
        max_event_age_seconds=user.max_event_age_seconds,
    )
    preview = {
        "key": key,
        "old_value": old_payload[key],
        "new_value": new_value,
        "old_display": format_setting_value(key, old_payload[key]),
        "new_display": format_setting_value(key, new_value),
        "effect": expected_effect(key, new_value),
        "warnings": warnings,
        "fingerprint": settings_fingerprint(old_payload),
        "proposed_payload": {**old_payload, key: new_value},
    }
    return True, None, preview


def apply_validated_payload(
    settings,
    telegram_id: int,
    proposed_payload: dict[str, Any],
    *,
    expected_fingerprint: str,
    changed_keys: list[str],
    action: str = "copy_settings_save",
) -> CopyUserSettings:
    current = get_user_settings(settings, telegram_id)
    old_payload = settings_to_payload(current)
    if settings_fingerprint(old_payload) != expected_fingerprint:
        raise ValueError("stale_preview")
    profile = proposed_payload.get("profile") or current.profile
    if profile not in PROFILE_DEFAULTS:
        profile = current.profile
    # Re-validate every editable key in proposed payload.
    validated: dict[str, Any] = {"profile": profile}
    for key in _SETTING_KEYS:
        result = parse_and_validate(key, proposed_payload[key])
        if not result.ok:
            raise ValueError(result.error or "invalid_settings")
        validated[key] = result.value
    updated = build_settings(
        telegram_id,
        profile,  # type: ignore[arg-type]
        global_max_event_age_seconds=int(settings.copy_max_event_age_seconds),
        **{k: validated[k] for k in _SETTING_KEYS},
    )
    save_user_settings(
        settings,
        updated,
        changed_keys=changed_keys,
        old_payload=old_payload,
        action=action,
    )
    return updated


def update_setting(settings, telegram_id: int, key: str, value) -> CopyUserSettings:
    """Immediate single-key update with validation (used by profile alert toggles / tests)."""
    ok, error, preview = preview_setting_change(settings, telegram_id, key, value)
    if not ok or preview is None:
        raise ValueError(error or "invalid_setting")
    return apply_validated_payload(
        settings,
        telegram_id,
        preview["proposed_payload"],
        expected_fingerprint=preview["fingerprint"],
        changed_keys=[key],
    )
