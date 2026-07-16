"""Trader analytics from closed positions, activity, and open positions."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from app.config.settings import Settings
from app.copy_intel.data_client import CopyDataClient, is_trade_activity
from app.copy_intel.models import TraderMetrics
from app.database import copy_storage

_WASH_WINDOW_SEC = 3600
_MM_CHURN_TRADES_PER_DAY = 50


def compute_trader_metrics(
    settings: Settings,
    wallet: str,
    client: CopyDataClient | None = None,
) -> TraderMetrics:
    client = client or CopyDataClient(settings)
    closed = client.fetch_closed_positions(wallet, limit=500)
    trades = client.fetch_trades(wallet, limit=500)
    activity = client.fetch_activity(wallet, limit=500)
    positions = client.fetch_positions(wallet, limit=100)

    trade_activity = [a for a in activity if is_trade_activity(a.get("type"))]
    resolved = [c for c in closed if c.get("realizedPnl") is not None]
    wins = sum(1 for c in resolved if float(c.get("realizedPnl") or 0) > 0)
    resolved_count = len(resolved)
    win_rate = wins / resolved_count if resolved_count else 0.0

    realized_pnl = sum(float(c.get("realizedPnl") or 0) for c in resolved)
    unrealized_pnl = sum(float(p.get("cashPnl") or 0) for p in positions)
    lifetime_pnl = realized_pnl + unrealized_pnl

    now = int(time.time())
    recent_cutoff = now - 30 * 86400
    recent_pnl = _recent_pnl_from_activity(trade_activity, recent_cutoff)

    avg_entry = _avg_entry(trades)
    avg_hold_hours = _avg_holding_hours(resolved)
    max_drawdown = _max_drawdown(resolved)
    concentration = _category_concentration(positions)
    wash_score = _wash_score(trades)

    trade_count = len([t for t in trades if t.get("side") in {"BUY", "SELL"}])
    recent_day_trades = _trades_last_24h(trades)
    eligible, reason = _eligibility_gate(
        settings,
        resolved_count=resolved_count,
        trade_count=trade_count,
        concentration=concentration,
        wash_score=wash_score,
        recent_day_trades=recent_day_trades,
    )

    metrics = TraderMetrics(
        wallet=wallet.lower(),
        trade_count=trade_count,
        resolved_count=resolved_count,
        win_rate=win_rate,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        avg_entry_price=avg_entry,
        avg_holding_hours=avg_hold_hours,
        max_drawdown=max_drawdown,
        category_concentration=concentration,
        recent_pnl_30d=recent_pnl,
        lifetime_pnl=lifetime_pnl,
        wash_score=wash_score,
        eligible=eligible,
        ineligible_reason=reason,
    )
    copy_storage.save_trader_metrics(
        settings,
        wallet,
        "lifetime",
        _metrics_to_dict(metrics),
    )
    return metrics


def _metrics_to_dict(metrics: TraderMetrics) -> dict[str, Any]:
    return {
        "trade_count": metrics.trade_count,
        "resolved_count": metrics.resolved_count,
        "win_rate": metrics.win_rate,
        "realized_pnl": metrics.realized_pnl,
        "unrealized_pnl": metrics.unrealized_pnl,
        "avg_entry_price": metrics.avg_entry_price,
        "avg_holding_hours": metrics.avg_holding_hours,
        "max_drawdown": metrics.max_drawdown,
        "category_concentration": metrics.category_concentration,
        "recent_pnl_30d": metrics.recent_pnl_30d,
        "lifetime_pnl": metrics.lifetime_pnl,
        "wash_score": metrics.wash_score,
        "eligible": metrics.eligible,
        "ineligible_reason": metrics.ineligible_reason,
    }


def _avg_entry(trades: list[dict]) -> float:
    prices = [float(t["price"]) for t in trades if t.get("price") is not None]
    return sum(prices) / len(prices) if prices else 0.0


def _avg_holding_hours(closed: list[dict]) -> float:
    # Data API lacks explicit open/close timestamps per position; approximate from endDate.
    return 24.0 if closed else 0.0


def _max_drawdown(closed: list[dict]) -> float:
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for item in closed:
        pnl = float(item.get("realizedPnl") or 0)
        cumulative += pnl
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def _category_concentration(positions: list[dict]) -> float:
    if not positions:
        return 0.0
    totals: Counter[str] = Counter()
    for pos in positions:
        slug = pos.get("eventSlug") or pos.get("slug") or "unknown"
        value = float(pos.get("currentValue") or pos.get("initialValue") or 0)
        totals[slug] += value
    total_value = sum(totals.values()) or 1.0
    return max(totals.values()) / total_value


def _wash_score(trades: list[dict]) -> float:
    if len(trades) < 4:
        return 0.0
    by_market: dict[str, list[dict]] = {}
    for trade in trades:
        key = trade.get("conditionId") or trade.get("asset") or ""
        by_market.setdefault(key, []).append(trade)
    suspicious = 0
    for market_trades in by_market.values():
        market_trades.sort(key=lambda t: int(t.get("timestamp") or 0))
        for i in range(len(market_trades) - 1):
            a, b = market_trades[i], market_trades[i + 1]
            ts_a = int(a.get("timestamp") or 0)
            ts_b = int(b.get("timestamp") or 0)
            if ts_b - ts_a <= _WASH_WINDOW_SEC and a.get("side") != b.get("side"):
                suspicious += 1
    return min(1.0, suspicious / max(1, len(trades)))


def _recent_pnl_from_activity(activity: list[dict], cutoff_ts: int) -> float:
    total = 0.0
    for item in activity:
        ts = int(item.get("timestamp") or 0)
        if ts < cutoff_ts:
            continue
        usdc = item.get("usdcSize")
        if usdc is not None:
            total += float(usdc) * (-1 if item.get("side") == "SELL" else 1)
    return total


def _trades_last_24h(trades: list[dict]) -> int:
    cutoff = int(time.time()) - 86400
    return sum(1 for t in trades if int(t.get("timestamp") or 0) >= cutoff)


def _eligibility_gate(
    settings: Settings,
    *,
    resolved_count: int,
    trade_count: int,
    concentration: float,
    wash_score: float,
    recent_day_trades: int,
) -> tuple[bool, str | None]:
    if resolved_count < settings.copy_min_resolved_trades:
        return False, f"min {settings.copy_min_resolved_trades} resolved trades required"
    if trade_count < settings.copy_min_resolved_trades:
        return False, "insufficient trade history"
    if concentration > 0.40:
        return False, "wallet too concentrated in one market"
    if wash_score > 0.25:
        return False, "suspicious wash-trading pattern"
    if recent_day_trades >= _MM_CHURN_TRADES_PER_DAY:
        return False, "market-maker style churn detected"
    return True, None
