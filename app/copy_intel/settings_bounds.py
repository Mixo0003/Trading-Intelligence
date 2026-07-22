"""Centralized bounds, labels, and validation for editable Copy Settings (V2.2.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.copy_intel.models import AlertDecisions

# Short callback field codes (keep Telegram callback_data under 64 bytes).
FIELD_CODES: dict[str, str] = {
    "mrm": "min_resolved_markets",
    "mad": "min_active_days",
    "mdd": "max_drawdown_pct",
    "msw": "max_single_win_contribution",
    "mcc": "max_category_concentration",
    "mlq": "min_liquidity_usd",
    "pis": "poll_interval_seconds",
    "age": "max_event_age_seconds",
    "alt": "alert_decisions",
}

FIELD_BY_KEY: dict[str, str] = {v: k for k, v in FIELD_CODES.items()}

EDITABLE_KEYS: frozenset[str] = frozenset(FIELD_CODES.values())

INT_KEYS = frozenset(
    {
        "min_resolved_markets",
        "min_active_days",
        "poll_interval_seconds",
        "max_event_age_seconds",
    }
)
FLOAT_KEYS = frozenset(
    {
        "max_drawdown_pct",
        "max_single_win_contribution",
        "max_category_concentration",
        "min_liquidity_usd",
    }
)
PERCENT_KEYS = frozenset(
    {
        "max_drawdown_pct",
        "max_single_win_contribution",
        "max_category_concentration",
    }
)
ENUM_KEYS = frozenset({"alert_decisions"})

ALERT_DECISIONS: frozenset[str] = frozenset({"FOLLOW_ONLY", "FOLLOW_AND_WATCH"})


@dataclass(frozen=True)
class SettingBound:
    key: str
    label: str
    unit: str
    min_value: float | int | None
    max_value: float | int | None
    value_type: str  # int | float | enum


BOUNDS: dict[str, SettingBound] = {
    "min_resolved_markets": SettingBound(
        "min_resolved_markets", "Min resolved markets", "count", 3, 500, "int"
    ),
    "min_active_days": SettingBound(
        "min_active_days", "Min active days", "days", 1, 3650, "int"
    ),
    "max_drawdown_pct": SettingBound(
        "max_drawdown_pct", "Max drawdown", "fraction", 0.05, 1.0, "float"
    ),
    "max_single_win_contribution": SettingBound(
        "max_single_win_contribution",
        "Max single-win share",
        "fraction",
        0.05,
        1.0,
        "float",
    ),
    "max_category_concentration": SettingBound(
        "max_category_concentration",
        "Max category concentration",
        "fraction",
        0.10,
        1.0,
        "float",
    ),
    "min_liquidity_usd": SettingBound(
        "min_liquidity_usd", "Min liquidity", "USD", 0, 1_000_000, "float"
    ),
    "poll_interval_seconds": SettingBound(
        "poll_interval_seconds", "Poll interval", "seconds", 60, 3600, "int"
    ),
    "max_event_age_seconds": SettingBound(
        "max_event_age_seconds", "Max event age", "seconds", 30, 86400, "int"
    ),
    "alert_decisions": SettingBound(
        "alert_decisions", "Alert mode", "enum", None, None, "enum"
    ),
}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    value: Any = None
    error: str | None = None
    warnings: tuple[str, ...] = ()


def resolve_field_key(code_or_key: str) -> str | None:
    if code_or_key in EDITABLE_KEYS:
        return code_or_key
    return FIELD_CODES.get(code_or_key)


def format_setting_value(key: str, value: Any) -> str:
    if key in PERCENT_KEYS:
        return f"{float(value) * 100:.0f}%"
    if key == "min_liquidity_usd":
        return f"${float(value):,.0f}"
    if key == "poll_interval_seconds":
        return f"{int(value)}s"
    if key == "max_event_age_seconds":
        return f"{int(value)}s"
    if key == "alert_decisions":
        return "FOLLOW only" if value == "FOLLOW_ONLY" else "FOLLOW+WATCH"
    if key == "min_active_days":
        return f"{int(value)} days"
    return str(value)


def expected_effect(key: str, new_value: Any) -> str:
    if key == "min_resolved_markets":
        return f"FOLLOW requires at least {int(new_value)} resolved markets."
    if key == "min_active_days":
        return f"FOLLOW requires at least {int(new_value)} active days."
    if key == "max_drawdown_pct":
        return f"FOLLOW rejects traders with drawdown above {float(new_value):.0%}."
    if key == "max_single_win_contribution":
        return f"FOLLOW rejects single-win share above {float(new_value):.0%}."
    if key == "max_category_concentration":
        return f"FOLLOW rejects category concentration above {float(new_value):.0%}."
    if key == "min_liquidity_usd":
        return f"Paper recommendation liquidity gate uses ${float(new_value):,.0f}."
    if key == "poll_interval_seconds":
        return f"Owned wallets skip poll cycles sooner than every {int(new_value)}s."
    if key == "max_event_age_seconds":
        return f"Alerts older than {int(new_value)}s are filtered for you."
    if key == "alert_decisions":
        if new_value == "FOLLOW_ONLY":
            return "Only FOLLOW recommendations are delivered to you."
        return "FOLLOW and WATCH recommendations are delivered to you."
    return "Setting will apply to Copy Intelligence for your user."


def collect_warnings(
    key: str,
    new_value: Any,
    *,
    poll_interval_seconds: int | None = None,
    global_poll_interval_seconds: int | None = None,
    max_event_age_seconds: int | None = None,
) -> list[str]:
    warnings: list[str] = []
    if key == "poll_interval_seconds" and global_poll_interval_seconds is not None:
        if int(new_value) < int(global_poll_interval_seconds):
            warnings.append(
                f"Effective poll cannot be faster than the global job floor "
                f"({int(global_poll_interval_seconds)}s)."
            )
    age = int(new_value) if key == "max_event_age_seconds" else max_event_age_seconds
    poll = int(new_value) if key == "poll_interval_seconds" else poll_interval_seconds
    if age is not None and poll is not None and int(age) < int(poll):
        warnings.append(
            "Max event age is shorter than your poll interval; many events may be missed."
        )
    if (
        age is not None
        and global_poll_interval_seconds is not None
        and int(age) < int(global_poll_interval_seconds)
    ):
        warnings.append(
            "Max event age is shorter than the global poll job interval; "
            "alerts may often be stale."
        )
    if key == "min_resolved_markets" and int(new_value) <= 5:
        warnings.append("Very low resolved-market gate may increase noisy FOLLOW candidates.")
    if key in PERCENT_KEYS and float(new_value) >= 0.80:
        warnings.append("High risk tolerance may admit unstable traders.")
    if key == "alert_decisions" and new_value == "FOLLOW_ONLY":
        warnings.append("WATCH paper alerts will be suppressed for you.")
    return warnings


def parse_and_validate(key: str, raw: Any) -> ValidationResult:
    if key not in EDITABLE_KEYS:
        return ValidationResult(ok=False, error=f"Unknown setting: {key}")

    bound = BOUNDS[key]
    if key == "alert_decisions":
        text = str(raw).strip().upper().replace(" ", "_").replace("+", "_AND_")
        aliases = {
            "FOLLOW_ONLY": "FOLLOW_ONLY",
            "FOLLOW": "FOLLOW_ONLY",
            "FOLLOW_AND_WATCH": "FOLLOW_AND_WATCH",
            "FOLLOW_WATCH": "FOLLOW_AND_WATCH",
            "BOTH": "FOLLOW_AND_WATCH",
        }
        value = aliases.get(text)
        if value not in ALERT_DECISIONS:
            return ValidationResult(
                ok=False,
                error="Alert mode must be FOLLOW_ONLY or FOLLOW_AND_WATCH",
            )
        return ValidationResult(ok=True, value=value)  # type: ignore[arg-type]

    if isinstance(raw, str):
        text = raw.strip().replace(",", "")
        if key in PERCENT_KEYS and text.endswith("%"):
            try:
                pct = float(text[:-1].strip())
            except ValueError:
                return ValidationResult(ok=False, error="Enter a valid percentage")
            if pct > 1.0:
                value: Any = pct / 100.0
            else:
                # Ambiguous 0–1 typed with %; treat as percent points if >1 already handled.
                value = pct / 100.0 if pct <= 100 else pct
            # If user typed "40%", value=0.40; if "0.40%", value=0.004 — accept only sensible.
            if value > 1.0:
                return ValidationResult(ok=False, error="Percentage must be between 5 and 100")
        elif key in PERCENT_KEYS:
            try:
                num = float(text)
            except ValueError:
                return ValidationResult(ok=False, error="Enter a valid number")
            # Accept 40 as 40% or 0.40 as fraction.
            value = num / 100.0 if num > 1.0 else num
        elif key in INT_KEYS:
            try:
                value = int(float(text))
            except ValueError:
                return ValidationResult(ok=False, error="Enter a whole number")
        else:
            try:
                value = float(text)
            except ValueError:
                return ValidationResult(ok=False, error="Enter a valid number")
    else:
        try:
            if key in INT_KEYS:
                value = int(raw)
            elif key in FLOAT_KEYS:
                value = float(raw)
            else:
                return ValidationResult(ok=False, error="Unsupported value type")
        except (TypeError, ValueError):
            return ValidationResult(ok=False, error="Invalid value type")

    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return ValidationResult(ok=False, error="Value must be a finite number")

    if bound.min_value is not None and value < bound.min_value:
        return ValidationResult(
            ok=False,
            error=f"{bound.label} must be >= {format_setting_value(key, bound.min_value)}",
        )
    if bound.max_value is not None and value > bound.max_value:
        return ValidationResult(
            ok=False,
            error=f"{bound.label} must be <= {format_setting_value(key, bound.max_value)}",
        )

    if key in INT_KEYS:
        value = int(value)
    elif key in FLOAT_KEYS:
        value = float(value)

    return ValidationResult(ok=True, value=value)


def preset_chips(key: str) -> list[tuple[str, Any]]:
    """Return (button_label, value) presets for a field."""
    if key == "alert_decisions":
        return [("FOLLOW only", "FOLLOW_ONLY"), ("FOLLOW+WATCH", "FOLLOW_AND_WATCH")]
    if key == "min_resolved_markets":
        return [("10", 10), ("20", 20), ("40", 40)]
    if key == "min_active_days":
        return [("14", 14), ("30", 30), ("60", 60)]
    if key == "max_drawdown_pct":
        return [("25%", 0.25), ("40%", 0.40), ("60%", 0.60)]
    if key == "max_single_win_contribution":
        return [("25%", 0.25), ("40%", 0.40), ("55%", 0.55)]
    if key == "max_category_concentration":
        return [("35%", 0.35), ("45%", 0.45), ("60%", 0.60)]
    if key == "min_liquidity_usd":
        return [("$200", 200.0), ("$500", 500.0), ("$1000", 1000.0)]
    if key == "poll_interval_seconds":
        return [("90s", 90), ("120s", 120), ("180s", 180)]
    if key == "max_event_age_seconds":
        return [("300s", 300), ("600s", 600), ("900s", 900)]
    return []
