"""Crypto-local Filters preview/confirm/cancel (not Polymarket oset:*)."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from app.config.settings import Settings
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    CryptoOpportunitySubscription,
    balanced_crypto_settings,
)
from app.crypto.venue_registry import V43_EXCHANGE_IDS
from app.database import crypto_opportunity_storage as storage
from app.opportunity_intel.settings import (
    ALLOCATION_PRESETS_USD,
    COOLDOWN_SECONDS_BOUNDS,
    MAXIMUM_SLIPPAGE_BOUNDS,
)

CONFIRM_TTL_SECONDS = 300
FIELD_CODES = frozenset({"ex", "pa", "np", "roi", "sl", "dep", "age", "cd"})
# Numeric Filters that accept presets + custom text (exchanges stay toggle-only).
NUMERIC_FIELDS = frozenset({"pa", "np", "roi", "sl", "dep", "age", "cd"})
INTEGER_FIELDS = frozenset({"age", "cd"})
FIELD_LABELS = {
    "ex": "Exchanges",
    "pa": "Paper allocation",
    "np": "Min net profit (USD)",
    "roi": "Min ROI",
    "sl": "Max slippage",
    "dep": "Min executable depth (USD)",
    "age": "Max signal age (seconds)",
    "cd": "Cooldown (seconds)",
}

# UI presets only — values must already pass CryptoOpportunitySettings validation.
_NET_PROFIT_PRESETS = (Decimal("0"), Decimal("1"), Decimal("5"), Decimal("10"), Decimal("25"))
_ROI_PRESETS = (
    Decimal("0"),
    Decimal("0.005"),
    Decimal("0.01"),
    Decimal("0.02"),
    Decimal("0.05"),
)
_SLIPPAGE_PRESETS = tuple(
    v
    for v in (
        Decimal("0.005"),
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.05"),
    )
    if MAXIMUM_SLIPPAGE_BOUNDS[0] <= v <= MAXIMUM_SLIPPAGE_BOUNDS[1]
)
_AGE_PRESETS = (10, 30, 60)
_COOLDOWN_PRESETS = tuple(
    v
    for v in (900, 1800, 3600, 7200, 86400)
    if COOLDOWN_SECONDS_BOUNDS[0] <= v <= COOLDOWN_SECONDS_BOUNDS[1]
)


@dataclass(frozen=True)
class CryptoFiltersConfirmation:
    token: str
    telegram_id: int
    starting_revision: int
    proposed: CryptoOpportunitySettings
    changed_keys: tuple[str, ...]
    preview_lines: tuple[str, ...]
    expires_at: float


_CONFIRMATIONS: dict[str, CryptoFiltersConfirmation] = {}


def clear_confirmations_for_tests() -> None:
    _CONFIRMATIONS.clear()


def drop_confirmations_for_user(telegram_id: int) -> None:
    doomed = [
        token
        for token, row in _CONFIRMATIONS.items()
        if row.telegram_id == int(telegram_id)
    ]
    for token in doomed:
        _CONFIRMATIONS.pop(token, None)


def load_settings(settings: Settings, telegram_id: int) -> CryptoOpportunitySettings:
    return storage.ensure_profile(settings, int(telegram_id))


def format_filters_summary(cfg: CryptoOpportunitySettings) -> str:
    venues = ", ".join(cfg.selected_exchanges)
    return "\n".join(
        [
            "🎛 Market Intelligence Filters",
            "Spot only · paper-only · delivery stays off.",
            "",
            f"Product type: Spot (fixed)",
            f"Exchanges: {venues}",
            f"Paper allocation: ${cfg.paper_allocation_usd}",
            f"Min net profit: ${cfg.minimum_net_profit_usd}",
            f"Min ROI: {cfg.minimum_roi}",
            f"Max slippage: {cfg.maximum_slippage}",
            f"Min executable depth: ${cfg.minimum_executable_depth_usd}",
            f"Max signal age: {cfg.maximum_signal_age_seconds}s",
            f"Cooldown: {cfg.cooldown_seconds}s",
            "",
            "Choose a field to edit. Changes require Confirm.",
        ]
    )


def presets_for_field(field: str) -> tuple[str, ...]:
    if field == "pa":
        return tuple(str(v) for v in ALLOCATION_PRESETS_USD)
    if field == "np":
        return tuple(str(v) for v in _NET_PROFIT_PRESETS)
    if field == "roi":
        return tuple(str(v) for v in _ROI_PRESETS)
    if field == "sl":
        return tuple(str(v) for v in _SLIPPAGE_PRESETS)
    if field == "dep":
        return tuple(str(v) for v in ALLOCATION_PRESETS_USD)
    if field == "age":
        return tuple(str(v) for v in _AGE_PRESETS)
    if field == "cd":
        return tuple(str(v) for v in _COOLDOWN_PRESETS)
    if field == "ex":
        return tuple(V43_EXCHANGE_IDS)
    return ()


def field_value_display(cfg: CryptoOpportunitySettings, field: str) -> str:
    if field == "ex":
        return ", ".join(cfg.selected_exchanges)
    if field == "pa":
        return f"${cfg.paper_allocation_usd}"
    if field == "np":
        return f"${cfg.minimum_net_profit_usd}"
    if field == "roi":
        return str(cfg.minimum_roi)
    if field == "sl":
        return str(cfg.maximum_slippage)
    if field == "dep":
        return f"${cfg.minimum_executable_depth_usd}"
    if field == "age":
        return f"{cfg.maximum_signal_age_seconds}s"
    if field == "cd":
        return f"{cfg.cooldown_seconds}s"
    return ""


def field_custom_prompt(field: str) -> str:
    """User-facing prompt for custom numeric input (existing validation only)."""
    label = FIELD_LABELS.get(field, field)
    if field == "pa":
        allowed = ", ".join(str(v) for v in ALLOCATION_PRESETS_USD)
        return (
            f"Enter custom {label}.\n"
            f"Send one approved allocation USD value: {allowed}\n"
            "Decimals allowed only as in the approved list. Confirm is still required."
        )
    if field == "np":
        return (
            f"Enter custom {label}.\n"
            "Send a non-negative number (USD). Example: 2.5\n"
            "Confirm is still required before save."
        )
    if field == "roi":
        return (
            f"Enter custom {label}.\n"
            "Send a non-negative decimal fraction. Example: 0.015\n"
            "Confirm is still required before save."
        )
    if field == "sl":
        lo, hi = MAXIMUM_SLIPPAGE_BOUNDS
        return (
            f"Enter custom {label}.\n"
            f"Send a decimal within [{lo}, {hi}]. Example: 0.015\n"
            "Confirm is still required before save."
        )
    if field == "dep":
        return (
            f"Enter custom {label}.\n"
            "Send a non-negative USD amount >= your paper allocation.\n"
            "Confirm is still required before save."
        )
    if field == "age":
        return (
            f"Enter custom {label}.\n"
            "Send a positive whole number of seconds. Example: 45\n"
            "Confirm is still required before save."
        )
    if field == "cd":
        lo, hi = COOLDOWN_SECONDS_BOUNDS
        return (
            f"Enter custom {label}.\n"
            f"Send a whole number of seconds within [{lo}, {hi}]. Example: 2700\n"
            "Confirm is still required before save."
        )
    raise ValueError("custom input not supported for this field")


def expected_format_hint(field: str) -> str:
    try:
        return field_custom_prompt(field)
    except ValueError:
        return "Invalid value."


def apply_field_set(
    current: CryptoOpportunitySettings,
    *,
    field: str,
    value: str,
) -> CryptoOpportunitySettings:
    if field not in FIELD_CODES:
        raise ValueError("unsupported field")
    if field == "ex":
        venue = value.strip().lower()
        if venue == "bybit" or venue not in V43_EXCHANGE_IDS:
            raise ValueError("exchange not in V4.3 universe")
        selected = set(current.selected_exchanges)
        if venue in selected:
            if len(selected) <= 1:
                raise ValueError("at least one exchange is required")
            selected.remove(venue)
        else:
            selected.add(venue)
        ordered = tuple(v for v in V43_EXCHANGE_IDS if v in selected)
        return replace(current, selected_exchanges=ordered)
    if field == "pa":
        allocation = Decimal(value)
        depth = max(current.minimum_executable_depth_usd, allocation)
        return replace(
            current,
            paper_allocation_usd=allocation,
            minimum_executable_depth_usd=depth,
        )
    if field == "np":
        return replace(current, minimum_net_profit_usd=Decimal(value))
    if field == "roi":
        return replace(current, minimum_roi=Decimal(value))
    if field == "sl":
        return replace(current, maximum_slippage=Decimal(value))
    if field == "dep":
        return replace(current, minimum_executable_depth_usd=Decimal(value))
    if field == "age":
        return replace(current, maximum_signal_age_seconds=int(value))
    if field == "cd":
        return replace(current, cooldown_seconds=int(value))
    raise ValueError("unsupported field")


def apply_custom_text(
    current: CryptoOpportunitySettings,
    *,
    field: str,
    text: str,
) -> CryptoOpportunitySettings:
    """Parse user text for a numeric field and validate via settings constructor."""
    if field not in NUMERIC_FIELDS:
        raise ValueError("custom input not supported for this field")
    raw = str(text).strip()
    if not raw:
        raise ValueError("empty value")
    try:
        number = parse_decimal(raw)
    except ValueError as exc:
        raise ValueError("malformed number") from exc
    if number < 0:
        raise ValueError("negative values are not allowed")
    if field in INTEGER_FIELDS:
        if number != number.to_integral_value():
            raise ValueError("whole number required")
        value = str(int(number))
    else:
        value = format(number, "f")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        if value == "-0":
            value = "0"
    # apply_field_set + constructor re-validation (existing bounds/presets).
    proposed = apply_field_set(current, field=field, value=value)
    return CryptoOpportunitySettings(
        revision=current.revision,
        selected_exchanges=proposed.selected_exchanges,
        selected_product_types=proposed.selected_product_types,
        paper_allocation_usd=proposed.paper_allocation_usd,
        minimum_net_profit_usd=proposed.minimum_net_profit_usd,
        minimum_roi=proposed.minimum_roi,
        maximum_slippage=proposed.maximum_slippage,
        minimum_executable_depth_usd=proposed.minimum_executable_depth_usd,
        maximum_signal_age_seconds=proposed.maximum_signal_age_seconds,
        risk_preference=proposed.risk_preference,
        evidence_requirements=proposed.evidence_requirements,
        delivery_enabled=False,
        cooldown_seconds=proposed.cooldown_seconds,
    )


def differing_numeric_keys(
    starting: CryptoOpportunitySettings,
    proposed: CryptoOpportunitySettings,
) -> tuple[str, ...]:
    keys: list[str] = []
    for field in ("ex", "pa", "np", "roi", "sl", "dep", "age", "cd"):
        if field_value_display(starting, field) != field_value_display(proposed, field):
            keys.append(field)
    return tuple(keys)


def build_preview_lines(
    starting: CryptoOpportunitySettings,
    proposed: CryptoOpportunitySettings,
    changed_keys: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    keys = list(changed_keys) or list(differing_numeric_keys(starting, proposed))
    # Include side-effects (e.g. depth bump when allocation rises).
    for extra in differing_numeric_keys(starting, proposed):
        if extra not in keys:
            keys.append(extra)
    lines = ["Confirm filter change?"]
    for key in keys:
        label = FIELD_LABELS.get(key, key)
        old = field_value_display(starting, key)
        new = field_value_display(proposed, key)
        lines.append(f"• {label}: {old} → {new}")
    lines.append("Confirm to save, or Cancel.")
    return tuple(lines)


def _with_delivery_forced_off(cfg: CryptoOpportunitySettings) -> CryptoOpportunitySettings:
    if cfg.delivery_enabled is False:
        return cfg
    return replace(cfg, delivery_enabled=False)


def create_confirmation(
    *,
    telegram_id: int,
    starting: CryptoOpportunitySettings,
    proposed: CryptoOpportunitySettings,
    changed_keys: list[str],
) -> CryptoFiltersConfirmation:
    proposed = _with_delivery_forced_off(
        replace(proposed, revision=starting.revision + 1)
    )
    # Re-validate via constructor path.
    proposed = CryptoOpportunitySettings(
        revision=proposed.revision,
        selected_exchanges=proposed.selected_exchanges,
        selected_product_types=proposed.selected_product_types,
        paper_allocation_usd=proposed.paper_allocation_usd,
        minimum_net_profit_usd=proposed.minimum_net_profit_usd,
        minimum_roi=proposed.minimum_roi,
        maximum_slippage=proposed.maximum_slippage,
        minimum_executable_depth_usd=proposed.minimum_executable_depth_usd,
        maximum_signal_age_seconds=proposed.maximum_signal_age_seconds,
        risk_preference=proposed.risk_preference,
        evidence_requirements=proposed.evidence_requirements,
        delivery_enabled=False,
        cooldown_seconds=proposed.cooldown_seconds,
    )
    keys = list(changed_keys) or list(differing_numeric_keys(starting, proposed))
    lines = build_preview_lines(starting, proposed, keys)
    drop_confirmations_for_user(telegram_id)
    token = secrets.token_hex(8)  # short: keeps callback_data under 64 bytes
    conf = CryptoFiltersConfirmation(
        token=token,
        telegram_id=int(telegram_id),
        starting_revision=starting.revision,
        proposed=proposed,
        changed_keys=tuple(sorted(set(keys))),
        preview_lines=lines,
        expires_at=time.time() + CONFIRM_TTL_SECONDS,
    )
    _CONFIRMATIONS[token] = conf
    return conf


def execute_confirmation(
    settings: Settings,
    *,
    telegram_id: int,
    token: str,
) -> tuple[str, CryptoOpportunitySettings | None]:
    conf = _CONFIRMATIONS.get(token)
    if conf is None:
        return "invalid_confirmation", None
    if conf.telegram_id != int(telegram_id):
        return "operator_mismatch", None
    if time.time() > conf.expires_at:
        _CONFIRMATIONS.pop(token, None)
        return "confirmation_expired", None
    current = load_settings(settings, telegram_id)
    if current.revision != conf.starting_revision:
        _CONFIRMATIONS.pop(token, None)
        return "stale_preview", None
    to_save = _with_delivery_forced_off(conf.proposed)
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(telegram_id=int(telegram_id), settings=to_save),
    )
    _CONFIRMATIONS.pop(token, None)
    return "ok", to_save


def cancel_confirmation(*, telegram_id: int, token: str) -> str:
    conf = _CONFIRMATIONS.get(token)
    if conf is None:
        return "invalid_confirmation"
    if conf.telegram_id != int(telegram_id):
        return "operator_mismatch"
    _CONFIRMATIONS.pop(token, None)
    return "cancelled"


def parse_decimal(text: str) -> Decimal:
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid number") from exc
    if not value.is_finite():
        raise ValueError("invalid number")
    return value
