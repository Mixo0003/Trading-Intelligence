"""Opaque preview/confirm tokens for Opportunity Scanner settings edits."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.config.settings import Settings
from app.database import opportunity_storage
from app.database.opportunity_storage import OpportunitySettingsRevisionConflict
from app.database.storage import upsert_user
from app.opportunity_intel.categories import (
    KNOWN_CATEGORY_IDS,
    validate_known_category_ids,
)
from app.opportunity_intel.display_labels import (
    alert_mode_label,
    live_mode_label,
    parse_alert_mode_text,
    parse_live_mode_text,
)
from app.opportunity_intel.models import LiveMode, OpportunitySubscription
from app.opportunity_intel.settings import (
    ALLOCATION_PRESETS_USD,
    COOLDOWN_SECONDS_BOUNDS,
    MAXIMUM_ABSOLUTE_SPREAD_BOUNDS,
    MAXIMUM_RELATIVE_SPREAD_BOUNDS,
    MAXIMUM_SLIPPAGE_BOUNDS,
    MINIMUM_24H_VOLUME_USD_BOUNDS,
    MINIMUM_LIQUIDITY_USD_BOUNDS,
    MINIMUM_SCORE_BOUNDS,
    CategoryAllowlist,
    OpportunityScannerSettings,
    QuietHours,
    balanced_settings,
)

ConfirmKind = Literal["edit", "reset"]
OPPORTUNITY_SETTINGS_CONFIRM_TTL_SECONDS = 600

_CONFIRMATIONS: dict[str, "OpportunitySettingsConfirmation"] = {}

FIELD_CODES: dict[str, str] = {
    "ms": "minimum_score",
    "ml": "minimum_liquidity_usd",
    "mv": "minimum_24h_volume_usd",
    "rs": "maximum_relative_spread",
    "as": "maximum_absolute_spread",
    "sl": "maximum_slippage",
    "pa": "paper_allocation_usd",
    "cd": "cooldown_seconds",
    "am": "alert_mode",
    "lm": "live_mode",
    "tz": "timezone_name",
    "qh": "quiet_hours",
}

FIELD_BY_KEY = {value: key for key, value in FIELD_CODES.items()}

FILTER_FIELD_KEYS: tuple[str, ...] = (
    "minimum_score",
    "minimum_liquidity_usd",
    "minimum_24h_volume_usd",
    "maximum_relative_spread",
    "maximum_absolute_spread",
    "maximum_slippage",
    "categories",
    "live_mode",
)

ALERT_FIELD_KEYS: tuple[str, ...] = (
    "alert_mode",
    "cooldown_seconds",
    "quiet_hours",
    "timezone_name",
    "paper_allocation_usd",
)

FIELD_LABELS: dict[str, str] = {
    "minimum_score": "Minimum score",
    "minimum_liquidity_usd": "Minimum liquidity",
    "minimum_24h_volume_usd": "Minimum 24h volume",
    "maximum_relative_spread": "Maximum relative spread",
    "maximum_absolute_spread": "Maximum absolute spread",
    "maximum_slippage": "Maximum slippage",
    "paper_allocation_usd": "Paper allocation",
    "categories": "Categories",
    "live_mode": "Live mode",
    "cooldown_seconds": "Cooldown",
    "alert_mode": "Alert mode",
    "quiet_hours": "Quiet hours",
    "timezone_name": "Timezone",
}

# Each field advertises only its own accepted input format. Ranges are derived
# from the unchanged validation bounds so prompt text cannot drift from them.
FIELD_INPUT_FORMATS: dict[str, tuple[str, str]] = {
    "minimum_score": (
        f"Enter a whole number from {MINIMUM_SCORE_BOUNDS[0]} "
        f"to {MINIMUM_SCORE_BOUNDS[1]}.",
        "65",
    ),
    "minimum_liquidity_usd": (
        f"Enter a USD amount from {MINIMUM_LIQUIDITY_USD_BOUNDS[0]} "
        f"to {MINIMUM_LIQUIDITY_USD_BOUNDS[1]}.",
        "10000",
    ),
    "minimum_24h_volume_usd": (
        f"Enter a USD amount from {MINIMUM_24H_VOLUME_USD_BOUNDS[0]} "
        f"to {MINIMUM_24H_VOLUME_USD_BOUNDS[1]}.",
        "25000",
    ),
    "maximum_relative_spread": (
        "Enter a spread fraction of midpoint from "
        f"{MAXIMUM_RELATIVE_SPREAD_BOUNDS[0]} to "
        f"{MAXIMUM_RELATIVE_SPREAD_BOUNDS[1]} (0.10 means 10%).",
        "0.10",
    ),
    "maximum_absolute_spread": (
        "Enter a probability-point value from "
        f"{MAXIMUM_ABSOLUTE_SPREAD_BOUNDS[0]} to "
        f"{MAXIMUM_ABSOLUTE_SPREAD_BOUNDS[1]}.",
        "0.05",
    ),
    "maximum_slippage": (
        f"Enter a slippage fraction from {MAXIMUM_SLIPPAGE_BOUNDS[0]} "
        f"to {MAXIMUM_SLIPPAGE_BOUNDS[1]} (0.02 means 2%).",
        "0.02",
    ),
    "paper_allocation_usd": (
        "Choose a preset USD amount.",
        "100",
    ),
    "cooldown_seconds": (
        f"Enter seconds from {COOLDOWN_SECONDS_BOUNDS[0]} "
        f"to {COOLDOWN_SECONDS_BOUNDS[1]}.",
        "1800",
    ),
    "quiet_hours": (
        "Enter HH:MM-HH:MM or off.",
        "23:00-07:00",
    ),
    "timezone_name": (
        "Enter a supported IANA timezone.",
        "America/Chicago or UTC",
    ),
    "categories": (
        "Select all categories or at least one category.",
        "politics",
    ),
    "live_mode": (
        "Choose OFF or ON.",
        "OFF",
    ),
    "alert_mode": (
        "Choose MANUAL or AUTO.",
        "MANUAL",
    ),
}


def field_label(field_key: str) -> str:
    return FIELD_LABELS.get(field_key, field_key)


def field_prompt(field_key: str) -> str:
    """Prompt showing only this field's accepted format and one example."""
    if field_key not in FIELD_INPUT_FORMATS:
        raise ValueError(f"unsupported field: {field_key}")
    instruction, example = FIELD_INPUT_FORMATS[field_key]
    return "\n".join(
        [
            field_label(field_key),
            instruction,
            f"Example: {example}",
        ]
    )


@dataclass(frozen=True)
class OpportunitySettingsConfirmation:
    token: str
    operator_id: str
    kind: ConfirmKind
    starting_revision: int
    proposed: OpportunityScannerSettings
    changed_keys: tuple[str, ...]
    expires_at: float
    preview_lines: tuple[str, ...]


def clear_confirmations_for_tests() -> None:
    _CONFIRMATIONS.clear()


def drop_operator_confirmations(operator_id: str | int) -> None:
    oid = str(operator_id)
    for token in [tok for tok, conf in _CONFIRMATIONS.items() if conf.operator_id == oid]:
        _CONFIRMATIONS.pop(token, None)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def resolve_field_key(code: str) -> str | None:
    return FIELD_CODES.get(code)


def get_or_default_subscription(
    settings: Settings, telegram_id: int
) -> OpportunitySubscription:
    existing = opportunity_storage.load_subscription(settings, telegram_id)
    if existing is not None:
        return existing
    return OpportunitySubscription(
        telegram_id=telegram_id,
        enabled=True,
        settings=balanced_settings(revision=0),
    )


def apply_live_mode_immediate(
    settings: Settings,
    *,
    telegram_id: int,
    live_mode: LiveMode,
) -> OpportunitySubscription:
    """Persist Live Mode immediately without a confirm token.

    Used by the Live Mode ON/OFF control screen. Preserves other fields and
    bumps revision when a row already exists.
    """
    current = get_or_default_subscription(settings, telegram_id)
    if (
        opportunity_storage.load_subscription(settings, telegram_id) is not None
        and current.settings.live_mode == live_mode
    ):
        return current

    upsert_user(settings, telegram_id, None, None)
    existing = opportunity_storage.load_subscription(settings, telegram_id)
    if existing is None:
        baseline = balanced_settings(revision=0)
        opportunity_storage.save_subscription(
            settings,
            OpportunitySubscription(
                telegram_id=telegram_id,
                enabled=True,
                settings=baseline,
            ),
            expected_revision=None,
        )
        if live_mode == baseline.live_mode:
            return OpportunitySubscription(
                telegram_id=telegram_id,
                enabled=True,
                settings=baseline,
            )
        proposed = replace(baseline, live_mode=live_mode, revision=1)
        new_sub = OpportunitySubscription(
            telegram_id=telegram_id,
            enabled=True,
            settings=proposed,
        )
        opportunity_storage.save_subscription(
            settings, new_sub, expected_revision=0
        )
        opportunity_storage.write_settings_audit(
            settings,
            telegram_id=telegram_id,
            from_revision=0,
            to_revision=1,
            changed_keys=["live_mode"],
            old_settings=baseline,
            new_settings=proposed,
        )
        return new_sub

    proposed = replace(
        existing.settings,
        live_mode=live_mode,
        revision=existing.settings.revision + 1,
    )
    new_sub = OpportunitySubscription(
        telegram_id=telegram_id,
        enabled=existing.enabled,
        settings=proposed,
    )
    opportunity_storage.save_subscription(
        settings, new_sub, expected_revision=existing.settings.revision
    )
    opportunity_storage.write_settings_audit(
        settings,
        telegram_id=telegram_id,
        from_revision=existing.settings.revision,
        to_revision=proposed.revision,
        changed_keys=["live_mode"],
        old_settings=existing.settings,
        new_settings=proposed,
    )
    return new_sub


def preview_field_change(
    current: OpportunityScannerSettings,
    *,
    field_key: str,
    raw_value: str,
) -> tuple[OpportunityScannerSettings, list[str]]:
    proposed = apply_field_value(current, field_key=field_key, raw_value=raw_value)
    proposed = replace(proposed, revision=current.revision + 1)
    lines = [
        "Confirm Opportunity Settings change?",
        f"Field: {field_label(field_key)}",
        f"{_display_value(current, field_key)} → {_display_value(proposed, field_key)}",
        f"Starting revision: {current.revision}",
        "Confirm to save, or Cancel.",
    ]
    return proposed, lines


def apply_field_value(
    current: OpportunityScannerSettings,
    *,
    field_key: str,
    raw_value: str,
) -> OpportunityScannerSettings:
    text = raw_value.strip()
    if field_key == "minimum_score":
        value = int(text)
        if not MINIMUM_SCORE_BOUNDS[0] <= value <= MINIMUM_SCORE_BOUNDS[1]:
            raise ValueError("minimum_score out of bounds")
        return replace(current, minimum_score=value)
    if field_key == "minimum_liquidity_usd":
        value = _decimal(text)
        _in_bounds(value, MINIMUM_LIQUIDITY_USD_BOUNDS)
        return replace(current, minimum_liquidity_usd=value)
    if field_key == "minimum_24h_volume_usd":
        value = _decimal(text)
        _in_bounds(value, MINIMUM_24H_VOLUME_USD_BOUNDS)
        return replace(current, minimum_24h_volume_usd=value)
    if field_key == "maximum_relative_spread":
        value = _decimal(text)
        _in_bounds(value, MAXIMUM_RELATIVE_SPREAD_BOUNDS)
        return replace(current, maximum_relative_spread=value)
    if field_key == "maximum_absolute_spread":
        value = _decimal(text)
        _in_bounds(value, MAXIMUM_ABSOLUTE_SPREAD_BOUNDS)
        return replace(current, maximum_absolute_spread=value)
    if field_key == "maximum_slippage":
        value = _decimal(text)
        _in_bounds(value, MAXIMUM_SLIPPAGE_BOUNDS)
        return replace(current, maximum_slippage=value)
    if field_key == "paper_allocation_usd":
        value = _decimal(text)
        if value not in ALLOCATION_PRESETS_USD:
            raise ValueError("allocation must be an approved preset")
        return replace(current, paper_allocation_usd=value)
    if field_key == "cooldown_seconds":
        value = int(text)
        if not COOLDOWN_SECONDS_BOUNDS[0] <= value <= COOLDOWN_SECONDS_BOUNDS[1]:
            raise ValueError("cooldown_seconds out of bounds")
        return replace(current, cooldown_seconds=value)
    if field_key == "alert_mode":
        return replace(current, alert_mode=parse_alert_mode_text(text))
    if field_key == "live_mode":
        return replace(current, live_mode=parse_live_mode_text(text))
    if field_key == "timezone_name":
        return replace(current, timezone_name=text)
    if field_key == "quiet_hours":
        if text.lower() in {"off", "none", "clear"}:
            return replace(current, quiet_hours=None)
        start_s, end_s = text.split("-", 1)
        from datetime import time as time_cls

        quiet = QuietHours(
            start=time_cls.fromisoformat(start_s.strip()),
            end=time_cls.fromisoformat(end_s.strip()),
        )
        return replace(current, quiet_hours=quiet)
    if field_key == "categories":
        if text.lower() == "all":
            return replace(current, categories=CategoryAllowlist())
        ids = frozenset(part.strip() for part in text.split(",") if part.strip())
        ids = validate_known_category_ids(ids)
        return replace(
            current,
            categories=CategoryAllowlist(all_categories=False, category_ids=ids),
        )
    raise ValueError(f"unsupported field: {field_key}")


def create_edit_confirmation(
    settings: Settings,
    *,
    operator_id: int,
    starting: OpportunityScannerSettings,
    proposed: OpportunityScannerSettings,
    changed_keys: list[str],
    preview_lines: list[str],
    kind: ConfirmKind = "edit",
) -> OpportunitySettingsConfirmation:
    if proposed.revision != starting.revision + 1:
        raise ValueError("proposed revision must follow starting revision")
    drop_operator_confirmations(operator_id)
    token = secrets.token_hex(16)
    expires_at = time.time() + OPPORTUNITY_SETTINGS_CONFIRM_TTL_SECONDS
    confirmation = OpportunitySettingsConfirmation(
        token=token,
        operator_id=str(operator_id),
        kind=kind,
        starting_revision=starting.revision,
        proposed=proposed,
        changed_keys=tuple(sorted(set(changed_keys))),
        expires_at=expires_at,
        preview_lines=tuple(preview_lines),
    )
    _CONFIRMATIONS[token] = confirmation
    opportunity_storage.save_setting_edit(
        settings,
        token_fingerprint=token_fingerprint(token),
        telegram_id=operator_id,
        starting_revision=starting.revision,
        proposed_settings=proposed,
        expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc),
    )
    return confirmation


def create_reset_confirmation(
    settings: Settings, *, operator_id: int
) -> OpportunitySettingsConfirmation:
    current = get_or_default_subscription(settings, operator_id).settings
    proposed = balanced_settings(revision=current.revision + 1)
    lines = [
        "Reset Opportunity Settings to Balanced?",
        f"Score: {current.minimum_score} → {proposed.minimum_score}",
        f"Allocation: ${current.paper_allocation_usd} → ${proposed.paper_allocation_usd}",
        f"Alert mode: {alert_mode_label(current.alert_mode)} → "
        f"{alert_mode_label(proposed.alert_mode)}",
        f"Live mode: {live_mode_label(current.live_mode)} → "
        f"{live_mode_label(proposed.live_mode)}",
        "Confirm to save, or Cancel.",
    ]
    return create_edit_confirmation(
        settings,
        operator_id=operator_id,
        starting=current,
        proposed=proposed,
        changed_keys=[
            "minimum_score",
            "minimum_liquidity_usd",
            "minimum_24h_volume_usd",
            "maximum_relative_spread",
            "maximum_absolute_spread",
            "maximum_slippage",
            "paper_allocation_usd",
            "categories",
            "live_mode",
            "cooldown_seconds",
            "alert_mode",
            "quiet_hours",
            "timezone_name",
        ],
        preview_lines=lines,
        kind="reset",
    )


def execute_confirmed_settings(
    settings: Settings,
    *,
    token: str,
    operator_id: int,
) -> tuple[str, OpportunitySubscription | None]:
    confirmation = _CONFIRMATIONS.get(token)
    if confirmation is None:
        return "invalid_confirmation", None
    if confirmation.operator_id != str(operator_id):
        return "operator_mismatch", None
    if time.time() > confirmation.expires_at:
        _CONFIRMATIONS.pop(token, None)
        return "confirmation_expired", None

    current_sub = opportunity_storage.load_subscription(settings, operator_id)
    if current_sub is None:
        if confirmation.starting_revision != 0:
            return "stale_preview", None
        old_settings = balanced_settings(revision=0)
        enabled = True
        # First persist: write revision 0 baseline, then apply proposed as revision 1.
        upsert_user(settings, operator_id, None, None)
        opportunity_storage.save_subscription(
            settings,
            OpportunitySubscription(
                telegram_id=operator_id,
                enabled=enabled,
                settings=old_settings,
            ),
            expected_revision=None,
        )
        expected = 0
    else:
        if current_sub.settings.revision != confirmation.starting_revision:
            return "stale_preview", None
        old_settings = current_sub.settings
        expected = confirmation.starting_revision
        enabled = current_sub.enabled
        upsert_user(settings, operator_id, None, None)

    # Revalidate categories against known catalog on confirm.
    proposed = confirmation.proposed
    if not proposed.categories.all_categories:
        validate_known_category_ids(proposed.categories.category_ids)

    new_sub = OpportunitySubscription(
        telegram_id=operator_id,
        enabled=enabled,
        settings=proposed,
    )
    try:
        opportunity_storage.save_subscription(
            settings, new_sub, expected_revision=expected
        )
    except OpportunitySettingsRevisionConflict:
        return "stale_preview", None

    opportunity_storage.write_settings_audit(
        settings,
        telegram_id=operator_id,
        from_revision=old_settings.revision,
        to_revision=proposed.revision,
        changed_keys=confirmation.changed_keys,
        old_settings=old_settings,
        new_settings=proposed,
    )
    opportunity_storage.consume_setting_edit(
        settings,
        token_fingerprint=token_fingerprint(token),
        telegram_id=operator_id,
        status="confirmed",
    )
    _CONFIRMATIONS.pop(token, None)
    return "ok", new_sub


def cancel_confirmation(
    settings: Settings, *, token: str, operator_id: int
) -> None:
    confirmation = _CONFIRMATIONS.pop(token, None)
    if confirmation is None:
        return
    if confirmation.operator_id != str(operator_id):
        return
    opportunity_storage.consume_setting_edit(
        settings,
        token_fingerprint=token_fingerprint(token),
        telegram_id=operator_id,
        status="cancelled",
    )


def toggle_category_draft(
    selected: set[str], category_id: str, *, enable: bool
) -> set[str]:
    if category_id not in KNOWN_CATEGORY_IDS:
        raise ValueError("unknown category")
    next_selected = set(selected)
    if enable:
        next_selected.add(category_id)
    else:
        next_selected.discard(category_id)
    return next_selected


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal") from exc


def _in_bounds(value: Decimal, bounds: tuple[Decimal, Decimal]) -> None:
    if not bounds[0] <= value <= bounds[1]:
        raise ValueError("value out of bounds")


def _display_value(settings: OpportunityScannerSettings, field_key: str) -> str:
    value = getattr(settings, field_key)
    if field_key == "quiet_hours":
        if value is None:
            return "off"
        return f"{value.start.isoformat(timespec='minutes')}-{value.end.isoformat(timespec='minutes')}"
    if field_key == "categories":
        if value.all_categories:
            return "all"
        return ",".join(sorted(value.category_ids))
    if field_key == "live_mode":
        return live_mode_label(value)
    if field_key == "alert_mode":
        return alert_mode_label(value)
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)
