"""Deterministic opportunity card and Telegram text formatting."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from app.opportunity_intel.display_labels import (
    ALERT_MODE_AUTO_DESCRIPTION,
    ALERT_MODE_AUTO_LABEL,
    ALERT_MODE_MANUAL_DESCRIPTION,
    ALERT_MODE_MANUAL_LABEL,
    LIVE_MODE_OFF_DESCRIPTION,
    LIVE_MODE_OFF_LABEL,
    LIVE_MODE_ON_DESCRIPTION,
    LIVE_MODE_ON_LABEL,
    alert_mode_label,
    live_mode_label,
)
from app.opportunity_intel.models import LiveMode, MovementDirection, ObservationMode
from app.opportunity_intel.payload import DeliveryCardPayload
from app.opportunity_intel.reason_text import reason_codes_to_uk_lines
from app.opportunity_intel.status_view import ScannerStatusView

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_plain(text: str, *, maximum: int = 500) -> str:
    """Strip control chars and Telegram markup metacharacters from untrusted text."""
    cleaned = _CONTROL_RE.sub("", text.replace("\r", ""))
    cleaned = cleaned.replace("*", "").replace("_", "").replace("`", "").replace("[", "(").replace("]", ")")
    cleaned = cleaned.replace("<", "(").replace(">", ")")
    return cleaned[:maximum]


def market_title_plain(text: str, *, maximum: int = 400) -> str:
    """Keep the original Polymarket title; strip only control characters."""
    return _CONTROL_RE.sub("", text.replace("\r", ""))[:maximum]


def format_opportunity_card(payload: DeliveryCardPayload, *, now: datetime | None = None) -> str:
    """Short Ukrainian Momentum / Top Opportunities card.

    Never emits raw JSON, dict repr, internal UIDs, or bare reason_codes.
    Market titles stay exactly as Polymarket provided them.
    """
    _ = now
    lines = [
        "📈 MOMENTUM СИГНАЛ",
        market_title_plain(payload.question, maximum=400),
        f"🎯 Сторона: {sanitize_plain(payload.outcome, maximum=80)}",
        f"💵 Вхід: {_pp(payload.execution_vwap)}",
        f"📊 Score: {_score(payload.canonical_score)}",
        (
            f"💰 ${_money(payload.allocation_usd)}: "
            f"VWAP {_pp(payload.execution_vwap)}, slippage {_pct(payload.slippage)}"
        ),
        "🧠 ВЕРДИКТ:",
        _momentum_verdict(payload),
        "Чому:",
    ]
    for reason in reason_codes_to_uk_lines(payload.reason_codes, limit=3):
        lines.append(f"✅ {reason}")
    if payload.market_link:
        lines.extend(["🔗 Відкрити на Polymarket", payload.market_link])
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
        text = text[: TELEGRAM_MAX_MESSAGE_LENGTH - 3] + "..."
    return text


def _momentum_verdict(payload: DeliveryCardPayload) -> str:
    moving = (
        "Ціна швидко рухається вгору."
        if payload.direction == MovementDirection.UP
        else "Ціна швидко рухається вниз."
    )
    if payload.mode == ObservationMode.LIVE_STRICT:
        return f"Live-ринок. {moving}"
    return moving


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
            f"Live mode: {live_mode_label(scanner.live_mode)}",
            (
                "🔥 Live Edge Opportunities: "
                f"{'✅ ON' if getattr(scanner, 'live_edge_enabled', False) else '❌ OFF'}"
            ),
            "Find underpriced live markets using verified event data.",
            "",
            "Filters decide which markets qualify.",
            "Alert timing lives under Alert Settings.",
        ]
    )


def format_alert_settings_card(
    scanner,
    *,
    user_delivery_available: bool,
    admin_delivery_available: bool = False,
    is_admin_viewer: bool = False,
    auto_alerts_enabled: bool = False,
    scanner_enabled: bool = False,
    worker_label: str = "Disabled",
) -> str:
    """When and how alerts may reach you. No market-filter fields here."""
    quiet = "off"
    if scanner.quiet_hours is not None:
        quiet = (
            f"{scanner.quiet_hours.start.isoformat(timespec='minutes')}-"
            f"{scanner.quiet_hours.end.isoformat(timespec='minutes')}"
        )
    mode_label = alert_mode_label(scanner.alert_mode)
    live_label = live_mode_label(scanner.live_mode)
    alert_note = _alert_delivery_note(
        alert_mode_label=mode_label,
        live_mode_label=live_label,
        is_admin_viewer=is_admin_viewer,
        admin_delivery_available=admin_delivery_available,
        user_delivery_available=user_delivery_available,
        auto_alerts_enabled=auto_alerts_enabled,
        scanner_enabled=scanner_enabled,
    )
    return "\n".join(
        [
            "Opportunity Alert Settings",
            f"Revision: {scanner.revision}",
            "",
            f"Alert mode: {mode_label}",
            f"Live mode: {live_label}",
            f"Cooldown: {scanner.cooldown_seconds}s",
            f"Quiet hours: {quiet}",
            f"Timezone: {scanner.timezone_name}",
            f"Paper allocation: ${_money(scanner.paper_allocation_usd)}",
            f"Scanner configuration: {'enabled' if scanner_enabled else 'disabled'}",
            f"Scanner worker: {worker_label}",
            (
                "Automatic delivery to regular users: "
                f"{'available' if user_delivery_available else 'disabled'}"
            ),
            (
                "Administrator delivery: "
                f"{'available' if admin_delivery_available else 'unavailable'}"
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
                f"{market_title_plain(title, maximum=120)} ({sanitize_plain(outcome, maximum=40)})",
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


def format_live_mode_screen(*, live_mode: LiveMode) -> str:
    """Minimal Live Mode control text — status only, no technical copy."""
    return "\n".join(
        [
            "Live Mode",
            f"Current status: {live_mode_label(live_mode)}",
        ]
    )


def format_live_edge_screen(*, enabled: bool) -> str:
    status = "✅ ON" if enabled else "❌ OFF"
    return "\n".join(
        [
            "🔥 Live Edge Opportunities",
            f"Current status: {status}",
            "",
            "Find underpriced live markets using verified event data.",
        ]
    )


def format_live_mode_help(
    *,
    live_mode: LiveMode | None = None,
    scanner_view: ScannerStatusView | None = None,
) -> str:
    """Compatibility alias for the Live Mode control screen."""
    _ = scanner_view
    return format_live_mode_screen(live_mode=live_mode or LiveMode.OFF)


def _alert_delivery_note(
    *,
    alert_mode_label: str,
    live_mode_label: str,
    is_admin_viewer: bool,
    admin_delivery_available: bool,
    user_delivery_available: bool,
    auto_alerts_enabled: bool,
    scanner_enabled: bool,
) -> str:
    if live_mode_label == LIVE_MODE_OFF_LABEL:
        return "Live Mode is OFF: new automatic alerts are not delivered."
    if alert_mode_label == ALERT_MODE_MANUAL_LABEL:
        return ALERT_MODE_MANUAL_DESCRIPTION
    if not scanner_enabled:
        return "Scanner is disabled: no new automatic alerts are delivered."
    if not auto_alerts_enabled:
        return "Automatic alerts are off for this process."
    if is_admin_viewer:
        if admin_delivery_available:
            return (
                f"{ALERT_MODE_AUTO_DESCRIPTION} "
                "Administrator delivery is available; "
                "regular-user delivery stays separate."
            )
        return (
            "Administrator delivery is unavailable "
            "(missing or disabled admin configuration)."
        )
    if user_delivery_available:
        return ALERT_MODE_AUTO_DESCRIPTION
    return "Automatic delivery to regular users is disabled."


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
    quantized = value.quantize(Decimal("0.01"))
    return f"{quantized:,.2f}"


def _ts_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _signal_family_label(value: str) -> str:
    return value.replace("_", " ").title()


# Re-export labels for tests / handlers that import from formatting.
__all__ = [
    "ALERT_MODE_AUTO_DESCRIPTION",
    "ALERT_MODE_AUTO_LABEL",
    "ALERT_MODE_MANUAL_DESCRIPTION",
    "ALERT_MODE_MANUAL_LABEL",
    "LIVE_MODE_OFF_DESCRIPTION",
    "LIVE_MODE_OFF_LABEL",
    "LIVE_MODE_ON_DESCRIPTION",
    "LIVE_MODE_ON_LABEL",
    "TELEGRAM_MAX_MESSAGE_LENGTH",
    "format_alert_settings_card",
    "format_filters_card",
    "format_live_mode_help",
    "format_opportunity_card",
    "format_signal_history",
    "market_title_plain",
    "sanitize_plain",
]
