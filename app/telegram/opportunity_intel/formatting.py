"""Deterministic opportunity card and Telegram text formatting."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from app.opportunity_intel.models import ObservationMode
from app.opportunity_intel.payload import DeliveryCardPayload
from app.opportunity_intel.reason_text import reason_codes_to_text

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_plain(text: str, *, maximum: int = 500) -> str:
    """Strip control chars and Telegram markup metacharacters from untrusted text."""
    cleaned = _CONTROL_RE.sub("", text.replace("\r", ""))
    cleaned = cleaned.replace("*", "").replace("_", "").replace("`", "").replace("[", "(").replace("]", ")")
    cleaned = cleaned.replace("<", "(").replace(">", ")")
    return cleaned[:maximum]


def format_opportunity_card(payload: DeliveryCardPayload, *, now: datetime | None = None) -> str:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    remaining = payload.end_at.astimezone(timezone.utc) - clock
    remaining_hours = max(0, int(remaining.total_seconds() // 3600))
    remaining_minutes = max(0, int((remaining.total_seconds() % 3600) // 60))
    signal_age_s = max(
        0, int((clock - payload.signal_at.astimezone(timezone.utc)).total_seconds())
    )
    lines = [
        "MARKET OPPORTUNITY",
        f"Score: {_score(payload.canonical_score)} / 100",
        "Executable Momentum (analysis only)",
    ]
    if payload.mode == ObservationMode.LIVE_STRICT:
        lines.extend(["LIVE / IN-PLAY", "STRICT DATA CHECKS"])
    lines.extend(
        [
            "",
            sanitize_plain(payload.question, maximum=400),
            f"Outcome: {sanitize_plain(payload.outcome, maximum=80)}",
            f"Midpoint: {_pp(payload.midpoint)}",
            f"Best ask: {_pp(payload.best_ask)}",
            f"Paper execution VWAP: {_pp(payload.execution_vwap)}",
            f"Paper allocation: ${_money(payload.allocation_usd)}",
            f"Absolute spread: {_pp(payload.absolute_spread)}",
            f"Relative spread: {_pct(payload.relative_spread)}",
            f"Allocation slippage: {_pct(payload.slippage)}",
            f"Liquidity: ${_money(payload.liquidity_usd)}",
            f"24h volume: ${_money(payload.volume_24h_usd)}",
            (
                f"Recent move 5m/15m/1h: {_move(payload.move_5m)} / "
                f"{_move(payload.move_15m)} / {_move(payload.move_1h)}"
            ),
            "Measurement windows: 5m / 15m / 1h (authoritative history timestamps)",
            f"Bid / Ask: {_pp(payload.best_bid)} / {_pp(payload.best_ask)}",
            f"Executable ask depth: ${_money(payload.executable_ask_depth_usd)}",
            f"Time remaining: {remaining_hours}h {remaining_minutes}m",
            f"Signal age: {signal_age_s}s",
            f"Reasons: {reason_codes_to_text(payload.reason_codes)}",
            (
                "Source data (UTC): "
                f"{payload.source_observed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            (
                "Disclaimer: analysis-only paper view of executable momentum and "
                "book quality. Not fair value, not arbitrage, no profit promise, "
                "and not a claim of true event probability."
            ),
        ]
    )
    if payload.market_link:
        lines.append(f"Polymarket: {payload.market_link}")
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
        text = text[: TELEGRAM_MAX_MESSAGE_LENGTH - 3] + "..."
    return text


def format_filters_card(scanner) -> str:
    """Which markets may qualify. No alert-scheduling fields here."""
    categories = (
        "all"
        if scanner.categories.all_categories
        else ", ".join(sorted(scanner.categories.category_ids))
    )
    return "\n".join(
        [
            "Opportunity Filters",
            f"Revision: {scanner.revision}",
            "",
            f"Minimum score: {scanner.minimum_score}",
            f"Minimum liquidity: ${_money(scanner.minimum_liquidity_usd)}",
            f"Minimum 24h volume: ${_money(scanner.minimum_24h_volume_usd)}",
            f"Maximum relative spread: {_pct(scanner.maximum_relative_spread)}",
            f"Maximum absolute spread: {_pp(scanner.maximum_absolute_spread)}",
            f"Maximum slippage: {_pct(scanner.maximum_slippage)}",
            f"Categories: {categories}",
            f"Live mode: {scanner.live_mode.value}",
            "",
            "Filters decide which markets qualify.",
            "Alert timing lives under Alert Settings.",
        ]
    )


def format_alert_settings_card(
    scanner,
    *,
    user_delivery_available: bool,
) -> str:
    """When and how alerts may reach you. No market-filter fields here."""
    quiet = "off"
    if scanner.quiet_hours is not None:
        quiet = (
            f"{scanner.quiet_hours.start.isoformat(timespec='minutes')}-"
            f"{scanner.quiet_hours.end.isoformat(timespec='minutes')}"
        )
    alert_note = (
        "Alerts will not be delivered while global user delivery is unavailable."
        if scanner.alert_mode.value == "INSTANT" and not user_delivery_available
        else (
            "Instant alerts depend on global user-delivery availability."
            if scanner.alert_mode.value == "INSTANT"
            else "On-demand only: no automatic push alerts."
        )
    )
    return "\n".join(
        [
            "Opportunity Alert Settings",
            f"Revision: {scanner.revision}",
            "",
            f"Alert mode: {scanner.alert_mode.value}",
            f"Cooldown: {scanner.cooldown_seconds}s",
            f"Quiet hours: {quiet}",
            f"Timezone: {scanner.timezone_name}",
            f"Paper allocation: ${_money(scanner.paper_allocation_usd)}",
            (
                "Global user delivery: "
                f"{'available' if user_delivery_available else 'unavailable'}"
            ),
            alert_note,
            "",
            "Market thresholds live under Filters.",
        ]
    )


def format_signal_history(signals, markets_by_asset: dict[str, tuple[str, str]]) -> str:
    if not signals:
        return "Signal History\n\nNo persisted signals yet."
    lines = ["Signal History", ""]
    for signal in signals:
        title, outcome = markets_by_asset.get(signal.asset_id, ("Market", "—"))
        lines.extend(
            [
                f"{sanitize_plain(title, maximum=120)} ({sanitize_plain(outcome, maximum=40)})",
                (
                    f"Score { _score(signal.score) } | {signal.direction.value} | "
                    f"{'LIVE' if signal.mode.value == 'LIVE_STRICT' else 'NORMAL'} | "
                    f"{signal.status.value}"
                ),
                (
                    "First seen (UTC): "
                    f"{signal.first_seen_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
                ),
                (
                    "Source (UTC): "
                    f"{signal.source_observed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
                ),
                "",
            ]
        )
    text = "\n".join(lines).rstrip()
    return text[:TELEGRAM_MAX_MESSAGE_LENGTH]


def format_live_mode_help() -> str:
    return "\n".join(
        [
            "Live Mode",
            "",
            "This screen configures filtering behaviour for live / in-play markets.",
            "It is not a list of active markets.",
            "",
            "OFF excludes live / in-play markets from your results.",
            "STRICT allows them only with stricter checks: stronger freshness,",
            "liquidity, spread, movement, and two-book consistency before a live",
            "signal can qualify.",
            "",
            "REST data uncertainty causes suppression.",
            "STRICT does not imply real-time certainty or a guaranteed opportunity.",
            "No trades are placed from this screen.",
        ]
    )


def _score(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.1")), "f")


def _pp(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


def _move(value: Decimal) -> str:
    scaled = (value * Decimal("100")).quantize(Decimal("0.01"))
    return f"{scaled}%"


def _pct(value: Decimal) -> str:
    scaled = (value * Decimal("100")).quantize(Decimal("0.01"))
    return f"{scaled}%"


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")
