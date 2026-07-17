"""Concise Telegram formatting for copy-trading alerts and discovery."""

from __future__ import annotations

from app.copy_intel.models import CopyRecommendation, CopyUserSettings, DiscoveryScore

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def format_recommendation_card(rec: CopyRecommendation) -> str:
    entry = _pct(rec.entry_price)
    current = _pct(rec.current_price)
    drift = _pct_signed(rec.price_drift)
    slip = _pct_signed(rec.slippage_est)
    lines = [
        f"{rec.action} · {rec.trader_label}",
        f"{_short(rec.market_title)} · {rec.outcome or '?'}",
        f"Entry {entry} -> Now {current} · Drift {drift}",
        f"Slippage {slip} · Score {rec.trader_score:.0f}",
        f"Max alloc ${rec.max_alloc:,.0f}",
        rec.reason,
    ]
    return "\n".join(lines)


def format_recommendation_messages(recs: list[CopyRecommendation]) -> list[str]:
    if not recs:
        return []
    chunks: list[str] = []
    current = "Copy Trading Alerts\n"
    for rec in recs:
        card = format_recommendation_card(rec) + "\n\n"
        if len(current) + len(card) > TELEGRAM_MAX_MESSAGE_LENGTH:
            chunks.append(current.rstrip())
            current = "Copy Trading Alerts\n" + card
        else:
            current += card
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


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
    if baseline not in {"COMPLETE", "PENDING", "FAILED", "RUNNING"}:
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


def format_copy_status(settings, poll_state: dict[str, str | None]) -> str:
    lines = [
        "Copy Intel Status",
        f"Enabled: {settings.copy_intel_enabled}",
        f"Read-only: {settings.copy_intel_read_only}",
        f"Poll interval: {settings.copy_poll_interval_seconds}s",
        f"Last poll: {poll_state.get('last_poll_at') or 'never'}",
        f"Last events: {poll_state.get('last_event_count') or '0'}",
    ]
    err = poll_state.get("last_error")
    if err:
        lines.append(f"Last error: {err[:120]}")
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


def format_settings_card(user: CopyUserSettings) -> str:
    return "\n".join(
        [
            "Copy Settings (no secrets)",
            f"Profile: {user.profile}",
            f"Min resolved markets: {user.min_resolved_markets}",
            f"Min active days: {user.min_active_days}",
            f"Max drawdown: {user.max_drawdown_pct:.0%}",
            f"Max single-win share: {user.max_single_win_contribution:.0%}",
            f"Max category concentration: {user.max_category_concentration:.0%}",
            f"Min liquidity: ${user.min_liquidity_usd:,.0f}",
            f"Poll interval: {user.poll_interval_seconds}s",
            f"Alert decisions: {user.alert_decisions}",
            "Default profile is Balanced.",
        ]
    )


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
