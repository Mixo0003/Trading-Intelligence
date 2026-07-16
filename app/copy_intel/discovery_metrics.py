"""Discovery-time trader metrics from public Polymarket data."""

from __future__ import annotations

import statistics
import time
from collections import Counter
from typing import Any

from app.copy_intel.data_client import CopyDataClient, is_trade_activity
from app.copy_intel.models import DiscoveryCandidate, DiscoveryMetrics

_WASH_WINDOW_SEC = 3600


def compute_discovery_metrics(
    candidate: DiscoveryCandidate,
    client: CopyDataClient,
    *,
    closed_limit: int = 200,
    trade_limit: int = 200,
) -> DiscoveryMetrics:
    wallet = candidate.wallet
    closed = client.fetch_closed_positions(wallet, limit=closed_limit)
    trades = client.fetch_trades(wallet, limit=trade_limit)
    activity = client.fetch_activity(wallet, limit=trade_limit)
    positions = client.fetch_positions(wallet, limit=100)

    incomplete: list[str] = []
    resolved = [c for c in closed if c.get("realizedPnl") is not None]
    resolved_count = len(resolved)

    if resolved_count == 0:
        incomplete.append("realized_pnl")
        incomplete.append("win_rate")
        incomplete.append("max_drawdown")
        incomplete.append("roi")

    realized_pnl = sum(float(c.get("realizedPnl") or 0) for c in resolved) if resolved else None
    if realized_pnl is None and candidate.leaderboard_pnl is not None:
        # Leaderboard PnL is public but may mix unrealized; mark incomplete.
        realized_pnl = float(candidate.leaderboard_pnl)
        incomplete.append("realized_pnl_leaderboard_proxy")

    total_bought = sum(float(c.get("totalBought") or 0) for c in resolved)
    roi = None
    if total_bought > 0 and realized_pnl is not None:
        roi = realized_pnl / total_bought
    else:
        incomplete.append("roi")

    wins = [c for c in resolved if float(c.get("realizedPnl") or 0) > 0]
    win_rate = (len(wins) / resolved_count) if resolved_count else None

    sizes = [
        float(t.get("size") or 0) * float(t.get("price") or 0)
        for t in trades
        if t.get("size") is not None and t.get("price") is not None
    ]
    sizes = [s for s in sizes if s > 0]
    avg_size = (sum(sizes) / len(sizes)) if sizes else None
    median_size = statistics.median(sizes) if sizes else None
    if not sizes:
        incomplete.append("position_size")

    max_dd = _max_drawdown_abs(resolved)
    if max_dd is None:
        incomplete.append("max_drawdown")

    peak = _peak_equity(resolved)
    max_dd_pct = (max_dd / peak) if (max_dd is not None and peak > 0) else max_dd

    active_days = _active_days(trades, activity)
    if active_days is None:
        incomplete.append("active_days")

    recent = _recent_pnl(activity, trades)
    lifetime = realized_pnl
    if lifetime is None:
        incomplete.append("lifetime_pnl")

    cats = _category_breakdown(closed, positions)
    main_cats = tuple(name for name, _ in cats.most_common(3))
    cat_conc = (cats.most_common(1)[0][1] / sum(cats.values())) if cats else None
    if cat_conc is None:
        incomplete.append("category_concentration")

    market_conc = _market_concentration(closed, positions)
    if market_conc is None:
        incomplete.append("market_concentration")

    single_win = _largest_win_contribution(resolved)
    if single_win is None:
        incomplete.append("single_win_contribution")

    liq = _liquidity_quality(positions, trades)
    if liq is None:
        incomplete.append("liquidity_quality")

    wash = _wash_score(trades)
    label = candidate.label or _pretty_label(wallet)

    return DiscoveryMetrics(
        wallet=wallet,
        label=label,
        realized_pnl=realized_pnl,
        roi=roi,
        resolved_count=resolved_count,
        active_days=active_days,
        win_rate=win_rate,
        avg_position_size=avg_size,
        median_position_size=median_size,
        max_drawdown=max_dd_pct,
        recent_pnl_30d=recent,
        lifetime_pnl=lifetime,
        main_categories=main_cats,
        category_concentration=cat_conc,
        market_concentration=market_conc,
        single_win_contribution=single_win,
        liquidity_quality=liq,
        wash_score=wash,
        incomplete_metrics=tuple(sorted(set(incomplete))),
        profile_url=f"https://polymarket.com/profile/{wallet}",
    )


def _pretty_label(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}"


def _max_drawdown_abs(closed: list[dict]) -> float | None:
    if not closed:
        return None
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for item in closed:
        cumulative += float(item.get("realizedPnl") or 0)
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def _peak_equity(closed: list[dict]) -> float:
    cumulative = 0.0
    peak = 0.0
    for item in closed:
        cumulative += float(item.get("realizedPnl") or 0)
        peak = max(peak, cumulative)
    return peak


def _active_days(trades: list[dict], activity: list[dict]) -> int | None:
    stamps: list[int] = []
    for item in trades + activity:
        ts = item.get("timestamp")
        if ts is not None:
            stamps.append(int(ts))
    if not stamps:
        return None
    span = max(stamps) - min(stamps)
    return max(1, int(span / 86400) + 1)


def _recent_pnl(activity: list[dict], trades: list[dict]) -> float | None:
    cutoff = int(time.time()) - 30 * 86400
    trade_activity = [a for a in activity if is_trade_activity(a.get("type"))]
    if not trade_activity and not trades:
        return None
    total = 0.0
    found = False
    for item in trade_activity:
        ts = int(item.get("timestamp") or 0)
        if ts < cutoff:
            continue
        found = True
        usdc = item.get("usdcSize")
        if usdc is not None:
            side = (item.get("side") or "").upper()
            total += float(usdc) * (-1 if side == "SELL" else 1)
    return total if found else 0.0


def _category_breakdown(closed: list[dict], positions: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for item in closed + positions:
        slug = item.get("eventSlug") or item.get("slug") or "unknown"
        # Use coarse prefix before first hyphen cluster as pseudo-category.
        category = str(slug).split("-")[0] if slug else "unknown"
        value = abs(float(item.get("realizedPnl") or item.get("currentValue") or item.get("initialValue") or 1))
        counts[category] += value
    return counts


def _market_concentration(closed: list[dict], positions: list[dict]) -> float | None:
    values: Counter[str] = Counter()
    for item in closed + positions:
        key = item.get("conditionId") or item.get("slug") or "unknown"
        values[key] += abs(float(item.get("realizedPnl") or item.get("currentValue") or 0))
    total = sum(values.values())
    if total <= 0:
        return None
    return max(values.values()) / total


def _largest_win_contribution(resolved: list[dict]) -> float | None:
    if not resolved:
        return None
    pnls = [float(c.get("realizedPnl") or 0) for c in resolved]
    wins = [p for p in pnls if p > 0]
    if not wins:
        return 0.0
    positive_sum = sum(wins)
    if positive_sum <= 0:
        return 0.0
    return max(wins) / positive_sum


def _liquidity_quality(positions: list[dict], trades: list[dict]) -> float | None:
    """Heuristic 0-1: higher when average trade notionals are moderate and positions varied."""
    notionals = [
        float(t.get("size") or 0) * float(t.get("price") or 0)
        for t in trades
        if t.get("size") is not None and t.get("price") is not None
    ]
    notionals = [n for n in notionals if n > 0]
    if not notionals and not positions:
        return None
    if not notionals:
        return 0.4
    avg = sum(notionals) / len(notionals)
    # Prefer mid-sized fills over tiny or huge outliers.
    if avg < 5:
        return 0.25
    if avg > 50000:
        return 0.35
    diversity = min(1.0, len({t.get("conditionId") for t in trades if t.get("conditionId")}) / 10)
    return round(0.4 + 0.6 * diversity, 3)


def _wash_score(trades: list[dict]) -> float:
    if len(trades) < 4:
        return 0.0
    by_market: dict[str, list[dict]] = {}
    for trade in trades:
        key = trade.get("conditionId") or trade.get("asset") or ""
        by_market.setdefault(str(key), []).append(trade)
    suspicious = 0
    for market_trades in by_market.values():
        market_trades.sort(key=lambda t: int(t.get("timestamp") or 0))
        for i in range(len(market_trades) - 1):
            a, b = market_trades[i], market_trades[i + 1]
            if int(b.get("timestamp") or 0) - int(a.get("timestamp") or 0) <= _WASH_WINDOW_SEC:
                if a.get("side") != b.get("side"):
                    suspicious += 1
    return min(1.0, suspicious / max(1, len(trades)))
