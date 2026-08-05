"""Telegram formatting for copy-trading alerts and discovery (V2.2.2 Checkpoint 10).

Alert cards are plaintext only — no Markdown/HTML parse mode, no inline buttons.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.copy_intel.models import CopyRecommendation, CopyUserSettings, DiscoveryScore
from app.copy_intel.recommendation import (
    REASON_COPY_ELIGIBLE,
    REASON_METRICS_INCOMPLETE,
    REASON_SCORE_BELOW_THRESHOLD,
    REASON_SLIPPAGE_LIMIT_EXCEEDED,
)
from app.polymarket.links import build_validated_market_link

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
POLYMARKET_HTTPS_HOST = "https://polymarket.com"
_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_MAX_LABEL_LEN = 96
_MAX_TITLE_LEN = 120
_MAX_REASON_LEN = 200

CODE_SKIP_NOT_RENDERABLE = "skip_not_renderable"
CODE_INVALID_MARKET_LINK = "invalid_market_link"
CODE_INVALID_TRADER_LINK = "invalid_trader_link"
CODE_MISSING_TRADE_TIME = "missing_trade_time"
CODE_INVALID_NUMERIC = "invalid_numeric_field"
CODE_FORMAT_FAILED = "alert_format_failed"

_REASON_PHRASES: dict[str, str] = {
    REASON_SLIPPAGE_LIMIT_EXCEEDED: "slippage above preferred limit",
    REASON_SCORE_BELOW_THRESHOLD: "score below threshold",
    REASON_METRICS_INCOMPLETE: "metrics incomplete",
    "price_drift_limit_exceeded": "price drift above preferred limit",
    "insufficient_depth": "insufficient visible ask depth",
    "below_min_liquidity": "below minimum liquidity",
    "quote_stale": "quote stale",
    "exposure_capacity_zero": "exposure capacity exhausted",
}


@dataclass(frozen=True)
class AlertFormatResult:
    ok: bool
    text: str | None = None
    code: str | None = None
    message: str = ""


def build_validated_trader_link(wallet: str | None) -> str | None:
    """Build ``https://polymarket.com/profile/{wallet}`` for a valid 0x address."""
    if wallet is None:
        return None
    cleaned = str(wallet).strip().lower()
    if not _WALLET_RE.match(cleaned):
        return None
    return f"{POLYMARKET_HTTPS_HOST}/profile/{cleaned}"


def format_deliverable_alert_card(
    rec: CopyRecommendation,
    *,
    now: float | None = None,
) -> AlertFormatResult:
    """Render a FOLLOW/WATCH plaintext card, or fail closed (never renders SKIP)."""
    action = getattr(rec, "action", None)
    if action == "SKIP":
        return AlertFormatResult(
            ok=False, code=CODE_SKIP_NOT_RENDERABLE, message="SKIP is never rendered"
        )
    if action not in {"COPY", "WATCH"}:
        return AlertFormatResult(
            ok=False, code=CODE_SKIP_NOT_RENDERABLE, message="unsupported decision"
        )

    market_link = build_validated_market_link(rec.market_slug)
    if market_link is None:
        return AlertFormatResult(
            ok=False, code=CODE_INVALID_MARKET_LINK, message="market slug invalid"
        )
    trader_link = build_validated_trader_link(rec.wallet)
    if trader_link is None:
        return AlertFormatResult(
            ok=False, code=CODE_INVALID_TRADER_LINK, message="wallet invalid"
        )

    first_ts = rec.first_fill_ts
    last_ts = rec.last_fill_ts
    if first_ts is None or last_ts is None:
        return AlertFormatResult(
            ok=False, code=CODE_MISSING_TRADE_TIME, message="authoritative trade time required"
        )
    try:
        first_i = int(first_ts)
        last_i = int(last_ts)
    except (TypeError, ValueError):
        return AlertFormatResult(
            ok=False, code=CODE_MISSING_TRADE_TIME, message="authoritative trade time invalid"
        )
    if first_i <= 0 or last_i <= 0:
        return AlertFormatResult(
            ok=False, code=CODE_MISSING_TRADE_TIME, message="authoritative trade time invalid"
        )

    # Required authoritative numerics for delivered cards.
    required_prices = (
        ("trader_vwap", rec.trader_vwap),
        ("midpoint", rec.midpoint),
        ("best_ask", rec.best_ask),
        ("execution_vwap", rec.execution_vwap),
    )
    for name, value in required_prices:
        if value is None or not _finite_number(value):
            return AlertFormatResult(
                ok=False, code=CODE_INVALID_NUMERIC, message=f"{name} unavailable"
            )
        if not (0.0 <= float(value) <= 1.0):
            return AlertFormatResult(
                ok=False, code=CODE_INVALID_NUMERIC, message=f"{name} out of range"
            )

    allocation = (
        rec.proposed_allocation_usd
        if rec.proposed_allocation_usd is not None
        else rec.max_alloc
    )
    bankroll = rec.paper_bankroll_usd
    share = rec.paper_bankroll_share
    if allocation is None or not _finite_number(allocation) or float(allocation) <= 0:
        return AlertFormatResult(
            ok=False, code=CODE_INVALID_NUMERIC, message="allocation unavailable"
        )
    if bankroll is None or not _finite_number(bankroll) or float(bankroll) <= 0:
        return AlertFormatResult(
            ok=False, code=CODE_INVALID_NUMERIC, message="paper bankroll unavailable"
        )
    if share is None or not _finite_number(share) or float(share) <= 0:
        # Derive share when missing but allocation/bankroll present.
        share = float(allocation) / float(bankroll)

    slippage = rec.slippage if rec.slippage is not None else rec.slippage_est
    if slippage is None or not _finite_number(slippage):
        return AlertFormatResult(
            ok=False, code=CODE_INVALID_NUMERIC, message="slippage unavailable"
        )
    depth = rec.visible_ask_depth_usd
    if depth is None or not _finite_number(depth) or float(depth) < 0:
        return AlertFormatResult(
            ok=False, code=CODE_INVALID_NUMERIC, message="visible ask depth unavailable"
        )

    size_delta = rec.size_delta
    resulting = rec.resulting_position_size
    if size_delta is None or not _finite_number(size_delta):
        return AlertFormatResult(
            ok=False, code=CODE_INVALID_NUMERIC, message="trade size unavailable"
        )
    if resulting is None or not _finite_number(resulting):
        return AlertFormatResult(
            ok=False, code=CODE_INVALID_NUMERIC, message="position after trade unavailable"
        )

    decision_label = "FOLLOW" if action == "COPY" else "WATCH"
    event_label = _event_type_label(rec.event_type)
    trader = _sanitize_label(rec.trader_label or rec.wallet[:10], _MAX_LABEL_LEN)
    title = _sanitize_label(rec.market_title or "Unknown market", _MAX_TITLE_LEN)
    outcome = _sanitize_label(rec.outcome or "?", 48)
    trade_time = _format_trade_time(first_i, last_i)
    age = _format_signal_age(last_i, now=now, event_age_seconds=rec.event_age_seconds)
    reason = _sanitize_label(build_reason_summary(rec), _MAX_REASON_LEN)

    lines = [
        f"{decision_label} · {trader}",
        f"{event_label} · {title} · {outcome}",
        f"Trade time: {trade_time}",
        f"Signal age: {age}",
        f"Trade size: {_format_signed_shares(float(size_delta))}",
        f"Position after trade: {_format_shares(float(resulting))}",
        f"Trader VWAP: {_format_price_cents(float(rec.trader_vwap))}",
        f"Market midpoint: {_format_price_cents(float(rec.midpoint))}",
        f"Executable best ask: {_format_price_cents(float(rec.best_ask))}",
        f"Paper allocation: {_format_usd(float(allocation))}",
        (
            f"Paper bankroll share: {_format_pct(float(share))} of "
            f"{_format_usd(float(bankroll))}"
        ),
        f"Paper execution VWAP: {_format_price_cents(float(rec.execution_vwap))}",
        f"Slippage: {_format_pct_signed(float(slippage))}",
        f"Visible ask depth: {_format_usd(float(depth))}",
        f"Reason: {reason}",
        f"Market: {market_link}",
        f"Trader: {trader_link}",
    ]
    if action == "WATCH":
        # Explicit: WATCH never implies a paper position was created.
        lines.append("No paper position opened.")

    text = "\n".join(lines)
    text = _normalize_plaintext(text)
    if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
        # Fail closed rather than silently dropping required fields.
        return AlertFormatResult(
            ok=False, code=CODE_FORMAT_FAILED, message="card exceeds telegram limit"
        )
    return AlertFormatResult(ok=True, text=text)


def format_recommendation_card(
    rec: CopyRecommendation, *, now: float | None = None
) -> str:
    """Compatibility wrapper — raises ValueError if the card cannot be rendered."""
    result = format_deliverable_alert_card(rec, now=now)
    if not result.ok or not result.text:
        raise ValueError(result.code or CODE_FORMAT_FAILED)
    return result.text


def format_recommendation_messages(recs: list[CopyRecommendation]) -> list[str]:
    """Batch deliverable cards. SKIP/failed formats are omitted. One card per message
    when a card would not fit with a header alongside others (clarity-first).
    """
    cards: list[str] = []
    for rec in recs:
        result = format_deliverable_alert_card(rec)
        if result.ok and result.text:
            cards.append(result.text)
    if not cards:
        return []
    # Prefer one alert per message for state clarity (Checkpoint 10).
    return [f"Copy Trading Alerts\n{card}" for card in cards]


def build_reason_summary(rec: CopyRecommendation) -> str:
    """Concise user-facing reason from sanitized reason codes + score."""
    codes = tuple(rec.reason_codes or ())
    score = float(rec.trader_score) if _finite_number(rec.trader_score) else None
    parts: list[str] = []
    if score is not None:
        parts.append(f"score {score:.0f}")

    if rec.action == "COPY" or REASON_COPY_ELIGIBLE in codes:
        parts.append("fresh complete book")
        parts.append("allocation within exposure limits")
    else:
        seen: set[str] = set()
        for code in codes:
            if code == REASON_COPY_ELIGIBLE:
                continue
            phrase = _REASON_PHRASES.get(str(code))
            if phrase and phrase not in seen:
                parts.append(phrase)
                seen.add(phrase)
        if not seen and rec.reason:
            # Fall back to already-sanitized engine reason without dumping internals.
            cleaned = _sanitize_label(str(rec.reason).split(":")[0], 80)
            if cleaned and cleaned not in parts:
                parts.append(cleaned)

    summary = "; ".join(parts) if parts else "evaluated"
    return summary


def _event_type_label(event_type: str | None) -> str:
    if event_type == "open":
        return "New Position"
    if event_type == "increase":
        return "Increase"
    return _sanitize_label(str(event_type or "Event"), 32)


def _format_trade_time(first_ts: int, last_ts: int) -> str:
    a = _format_epoch_utc(first_ts)
    b = _format_epoch_utc(last_ts)
    if first_ts == last_ts:
        return f"{a} UTC"
    return f"{a}–{b} UTC"


def _format_epoch_utc(ts: int) -> str:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _format_signal_age(
    last_fill_ts: int,
    *,
    now: float | None,
    event_age_seconds: float | None,
) -> str:
    # Always derive from authoritative last_fill_ts (never detected_at).
    now_ts = float(time.time() if now is None else now)
    age = max(0.0, now_ts - float(last_fill_ts))
    if event_age_seconds is not None and _finite_number(event_age_seconds):
        # Prefer live clock; event_age_seconds is informational only when now provided.
        pass
    if age < 60:
        return f"{int(round(age))}s"
    minutes = age / 60.0
    if minutes < 60:
        if abs(minutes - round(minutes)) < 1e-9:
            return f"{int(round(minutes))}m"
        return f"{minutes:.1f}m"
    hours = minutes / 60.0
    return f"{hours:.1f}h"


def _format_price_cents(value: float) -> str:
    return f"{value * 100:.1f}¢"


def _format_usd(value: float) -> str:
    return f"${value:,.2f}"


def _format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _format_pct_signed(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _format_shares(value: float) -> str:
    return f"{_format_share_number(value)} shares"


def _format_signed_shares(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{_format_share_number(abs(value))} shares"


def _format_share_number(value: float) -> str:
    """Preserve up to six decimals; strip trailing zeros when unambiguous."""
    quantized = f"{value:.6f}".rstrip("0").rstrip(".")
    if "." in quantized:
        whole, frac = quantized.split(".", 1)
        whole_fmt = f"{int(whole):,}" if whole.lstrip("-").isdigit() else whole
        return f"{whole_fmt}.{frac}"
    try:
        return f"{int(round(float(quantized))):,}"
    except ValueError:
        return quantized


def _finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _sanitize_label(text: str, limit: int) -> str:
    cleaned = _normalize_plaintext(str(text))
    cleaned = "".join(
        ch for ch in cleaned if not unicodedata.category(ch).startswith("C")
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)] + "…"


def _normalize_plaintext(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        ch if ch == "\n" or not unicodedata.category(ch).startswith("C") else " "
        for ch in text
    )
    # Collapse runs of spaces/tabs but preserve newlines.
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(lines).strip()


def format_watchlist(entries: list, metrics_by_wallet: dict[str, float]) -> str:
    """Legacy compact list (slash-command fallback)."""
    if not entries:
        return format_watchlist_empty()
    lines = ["👀 Watchlist"]
    for entry in entries:
        status = "ON" if entry.enabled else "PAUSED"
        label = entry.label or entry.wallet[:10]
        score = metrics_by_wallet.get(entry.wallet)
        score_text = f"score {score:.0f}" if score is not None else "score —"
        lines.append(f"• {label} ({status}) {score_text}")
        lines.append(f"  {_short_wallet(entry.wallet)}")
    return "\n".join(lines)


def format_watchlist_empty() -> str:
    return (
        "👀 Watchlist\n"
        "No subscribed traders yet.\n"
        "Use Trader Discovery to add one."
    )


def format_subscription_card(
    *,
    label: str | None,
    wallet: str,
    enabled: bool,
    added_at: str | None,
    baseline_status: str | None,
    last_poll_at: str | None,
    last_new_event_at: str | None,
    alert_decisions: str,
    profile_url: str,
    index: int,
    total: int,
) -> str:
    monitoring = "ON" if enabled else "PAUSED"
    baseline = (baseline_status or "PENDING").upper()
    if baseline not in {"COMPLETE", "PENDING", "FAILED", "RUNNING", "MIGRATING"}:
        baseline = "PENDING"
    decision = (
        "FOLLOW only"
        if alert_decisions == "FOLLOW_ONLY"
        else "FOLLOW + WATCH"
    )
    lines = [
        f"👀 Watchlist ({index + 1}/{total})",
        "",
        f"👤 {label or _short_wallet(wallet)}",
        f"Wallet: {_short_wallet(wallet)}",
        f"Monitoring: {monitoring}",
        f"Added: {_fmt_ts(added_at)}",
        f"Baseline: {baseline}",
        f"Last poll: {_fmt_ts(last_poll_at) if last_poll_at else 'Never'}",
        f"Last new event: {_fmt_ts(last_new_event_at) if last_new_event_at else 'None'}",
        f"Decision filter: {decision}",
        f"Profile: {profile_url}",
    ]
    return "\n".join(lines)


def format_unsubscribe_confirm(*, label: str | None, wallet: str) -> str:
    return (
        "🗑 Unsubscribe\n"
        f"Remove {label or _short_wallet(wallet)} from your Watchlist?\n"
        "This stops future polling for this subscription.\n"
        "History may be retained for audit/dedup."
    )


def format_copy_status(
    settings,
    poll_state: dict[str, str | None],
    *,
    scheduler_registered: bool = False,
    scheduler_skip_reason: str | None = None,
    watchlist_total: int = 0,
    watchlist_enabled: int = 0,
    watchlist_paused: int = 0,
    baseline_complete: int = 0,
    baseline_pending: int = 0,
    delivery_pending: int = 0,
) -> str:
    """Admin status screen — never imply polling is active when it is not."""
    if not settings.copy_intel_enabled:
        polling = "DISABLED — COPY_INTEL_ENABLED is false (no poll job registered)"
    elif scheduler_skip_reason:
        polling = f"DISABLED — {scheduler_skip_reason}"
    elif scheduler_registered:
        polling = "ACTIVE — scheduler registered once"
    else:
        polling = "INACTIVE — scheduler not registered"

    lines = [
        "Copy Intel Status",
        f"COPY_INTEL_ENABLED: {settings.copy_intel_enabled}",
        f"Polling: {polling}",
        f"Read-only: {settings.copy_intel_read_only}",
        f"Poll interval: {settings.copy_poll_interval_seconds}s",
        (
            f"Watchlist: {watchlist_total} total "
            f"({watchlist_enabled} enabled, {watchlist_paused} paused)"
        ),
        (
            f"Baseline: {baseline_complete} complete, "
            f"{baseline_pending} pending/other"
        ),
        f"Last poll: {poll_state.get('last_poll_at') or 'never'}",
        f"Last event count: {poll_state.get('last_event_count') or '0'}",
        f"Delivery pending: {delivery_pending}",
    ]
    err = poll_state.get("last_error")
    if err:
        lines.append(f"Last error: {err[:120]}")
    elif not settings.copy_intel_enabled:
        lines.append("Last error: (none — polling disabled by config)")
    return "\n".join(lines)


def format_paper_portfolio(bankroll: float, exposure: dict) -> str:
    lines = [
        "Paper Portfolio",
        f"Bankroll: ${bankroll:,.0f}",
        f"Deployed: ${exposure['total']:,.0f}",
    ]
    for wallet, amount in exposure.get("by_wallet", {}).items():
        lines.append(f"• {wallet[:10]}… ${amount:,.0f}")
    if not exposure.get("by_wallet"):
        lines.append("No open paper positions.")
    return "\n".join(lines)


def format_recent_alerts(rows: list) -> str:
    if not rows:
        return "No copy alerts yet."
    lines = ["Recent Copy Alerts"]
    for row in rows[:10]:
        lines.append(
            f"• {row['action']} {row['market_title'] or row['market_slug']} "
            f"(score {row['trader_score'] or 0:.0f})"
        )
    return "\n".join(lines)


def format_trader_card(score: DiscoveryScore) -> str:
    m = score.metrics
    incomplete = ", ".join(m.incomplete_metrics[:4]) if m.incomplete_metrics else "none"
    cats = ", ".join(m.main_categories[:3]) if m.main_categories else "—"
    lines = [
        f"Trader: {score.label or m.wallet[:10]}",
        f"PnL: {_money(m.realized_pnl)}",
        f"ROI: {_pct_or_na(m.roi)}",
        f"Resolved markets: {m.resolved_count}",
        f"Active period: {_days(m.active_days)}",
        f"Win rate: {_pct_or_na(m.win_rate)}",
        f"Drawdown: {_pct_or_na(m.max_drawdown)}",
        f"Main categories: {cats}",
        f"Concentration: {_pct_or_na(m.category_concentration)}",
        f"Risk: {score.risk_level}",
        f"Confidence: {score.data_confidence}",
        f"Score: {score.final_score:.0f}/100",
        f"Decision: {score.decision.title()}",
        f"Reason: {score.reason}",
        f"Profile: {m.profile_url}",
        f"Incomplete: {incomplete}",
    ]
    return "\n".join(lines)


def format_trader_list(scores: list[DiscoveryScore], *, title: str) -> list[str]:
    if not scores:
        return [f"{title}\nNo traders matched filters."]
    chunks: list[str] = []
    current = f"{title}\n"
    for score in scores:
        card = format_trader_card(score) + "\n\n"
        if len(current) + len(card) > TELEGRAM_MAX_MESSAGE_LENGTH:
            chunks.append(current.rstrip())
            current = f"{title}\n" + card
        else:
            current += card
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def format_settings_card(user: CopyUserSettings, *, modified: bool = False) -> str:
    profile_line = f"Profile: {user.profile}"
    if modified:
        profile_line += " (Modified)"
    return "\n".join(
        [
            "Copy Settings (no secrets)",
            profile_line,
            f"Min resolved markets: {user.min_resolved_markets}",
            f"Min active days: {user.min_active_days}",
            f"Max drawdown: {user.max_drawdown_pct:.0%}",
            f"Max single-win share: {user.max_single_win_contribution:.0%}",
            f"Max category concentration: {user.max_category_concentration:.0%}",
            f"Min liquidity: ${user.min_liquidity_usd:,.0f}",
            f"Poll interval: {user.poll_interval_seconds}s",
            f"Max event age: {user.max_event_age_seconds}s",
            f"Alert decisions: {user.alert_decisions}",
            "Default profile is Balanced.",
            "Use Edit Settings to change one value, or Reset to Balanced.",
        ]
    )


def format_settings_edit_list() -> str:
    return "\n".join(
        [
            "Edit Copy Settings",
            "Choose one setting to change.",
            "You will preview and confirm before anything is saved.",
        ]
    )


def format_settings_field_prompt(user: CopyUserSettings, key: str) -> str:
    from app.copy_intel.settings_bounds import BOUNDS, format_setting_value

    bound = BOUNDS[key]
    current = getattr(user, key)
    lines = [
        f"Edit: {bound.label}",
        f"Current: {format_setting_value(key, current)}",
        f"Unit: {bound.unit}",
    ]
    if bound.min_value is not None and bound.max_value is not None:
        lines.append(
            f"Allowed: {format_setting_value(key, bound.min_value)}"
            f" – {format_setting_value(key, bound.max_value)}"
        )
    lines.append("Tap a preset below, or type a new value in chat.")
    return "\n".join(lines)


def format_settings_preview(
    *,
    key: str,
    old_display: str,
    new_display: str,
    effect: str,
    warnings: list[str],
) -> str:
    from app.copy_intel.settings_bounds import BOUNDS

    lines = [
        "Preview change (not saved yet)",
        f"Setting: {BOUNDS[key].label}",
        f"Old: {old_display}",
        f"New: {new_display}",
        f"Effect: {effect}",
    ]
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {w}" for w in warnings)
    else:
        lines.append("Warnings: none")
    lines.append("Confirm to save, or Cancel to discard.")
    return "\n".join(lines)


def _money(value: float | None) -> str:
    if value is None:
        return "incomplete"
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}"


def _pct_or_na(value: float | None) -> str:
    if value is None:
        return "incomplete"
    return f"{value * 100:.1f}%"


def _days(value: int | None) -> str:
    if value is None:
        return "incomplete"
    return f"{value}d"


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _pct_signed(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.1f}%"


def _short(text: str, limit: int = 48) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_wallet(wallet: str) -> str:
    w = wallet or ""
    if len(w) <= 12:
        return w
    return f"{w[:6]}…{w[-4:]}"


def _fmt_ts(value: str | None) -> str:
    if not value:
        return "—"
    text = value.replace("Z", "+00:00")
    if "T" in text:
        date_part, time_part = text.split("T", 1)
        time_part = time_part.split(".")[0]
        time_part = time_part.replace("+00:00", "").replace("-00:00", "")
        if "+" in time_part:
            time_part = time_part.split("+", 1)[0]
        elif time_part.count("-") > 0 and ":" in time_part:
            # strip trailing offset like -05:00
            maybe = time_part.rsplit("-", 1)
            if len(maybe) == 2 and ":" in maybe[1]:
                time_part = maybe[0]
        return f"{date_part} {time_part[:8]} UTC"
    return text[:19]
