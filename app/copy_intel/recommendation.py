"""COPY / WATCH / SKIP recommendation engine."""

from __future__ import annotations

import time

from app.config.settings import Settings
from app.copy_intel.analytics import compute_trader_metrics
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.models import CopyAction, CopyRecommendation, TradeEvent
from app.copy_intel.risk import check_exposure_limits, estimate_slippage
from app.copy_intel.scoring import score_trader
from app.database import copy_storage


def build_recommendation(
    settings: Settings,
    event: TradeEvent,
    *,
    trader_label: str | None = None,
    client: CopyDataClient | None = None,
) -> CopyRecommendation:
    client = client or CopyDataClient(settings)
    metrics = compute_trader_metrics(settings, event.wallet, client=client)
    trader_score = score_trader(metrics)

    entry_price = event.price
    current_price = _resolve_current_price(client, event)
    price_drift = _price_drift(entry_price, current_price)
    slippage_est = estimate_slippage(client, event.asset, side=event.side or "BUY")

    label = trader_label or event.wallet[:10]
    action, reason, max_alloc = _decide_action(
        settings,
        event=event,
        metrics=metrics,
        trader_score=trader_score,
        price_drift=price_drift,
        slippage_est=slippage_est,
        current_price=current_price,
    )

    rec = CopyRecommendation(
        wallet=event.wallet,
        trader_label=label,
        market_title=event.title or "Unknown market",
        market_slug=event.slug,
        outcome=event.outcome,
        event_type=event.event_type,
        entry_price=entry_price,
        current_price=current_price,
        price_drift=price_drift,
        slippage_est=slippage_est,
        trader_score=trader_score,
        max_alloc=max_alloc,
        action=action,
        reason=reason,
    )
    _persist_recommendation(settings, rec)
    return rec


def _resolve_current_price(client: CopyDataClient, event: TradeEvent) -> float | None:
    if event.asset:
        quote = client.fetch_quote(event.asset)
        return quote.midpoint or quote.best_ask or quote.best_bid
    return None


def _price_drift(entry: float | None, current: float | None) -> float | None:
    if entry is None or current is None or entry <= 0:
        return None
    return (current - entry) / entry


def _decide_action(
    settings: Settings,
    *,
    event: TradeEvent,
    metrics,
    trader_score: float,
    price_drift: float | None,
    slippage_est: float | None,
    current_price: float | None,
) -> tuple[CopyAction, str, float]:
    bankroll = settings.copy_paper_bankroll_usd
    base_alloc = bankroll * 0.03

    if event.event_type in {"close", "reduce"}:
        return "SKIP", "position reduced or closed by trader", 0.0

    if event.ts and (int(time.time()) - int(event.ts)) > settings.copy_max_trade_age_min * 60:
        return "SKIP", "stale trade beyond age limit", 0.0

    if slippage_est is not None and slippage_est > settings.copy_max_slippage_pct:
        return "WATCH", f"slippage {slippage_est:.1%} above limit", base_alloc * 0.5

    if price_drift is not None and abs(price_drift) > settings.copy_max_slippage_pct:
        return "WATCH", f"price drift {price_drift:.1%} since entry", base_alloc * 0.5

    liquidity_ok, liq_reason = _liquidity_ok(settings, slippage_est)
    if not liquidity_ok:
        return "SKIP", liq_reason, 0.0

    exposure_ok, exposure_reason, cap = check_exposure_limits(
        settings,
        wallet=event.wallet,
        condition_id=event.condition_id or "",
        proposed_notional=base_alloc,
    )
    if not exposure_ok:
        return "WATCH", exposure_reason, cap

    if not metrics.eligible:
        return "WATCH", metrics.ineligible_reason or "trader below quality gate", cap

    if trader_score < 55:
        return "WATCH", f"trader score {trader_score:.0f} below COPY threshold", cap

    if current_price is None:
        return "WATCH", "current price unavailable", cap

    return "COPY", f"score {trader_score:.0f}; drift {price_drift or 0:.1%}", cap


def _liquidity_ok(settings: Settings, slippage_est: float | None) -> tuple[bool, str]:
    if slippage_est is None:
        return True, ""
    depth_proxy = (1.0 - min(1.0, slippage_est * 10)) * settings.copy_min_liquidity_usd
    if depth_proxy < settings.copy_min_liquidity_usd * 0.25:
        return False, "insufficient market liquidity"
    return True, ""


def _persist_recommendation(settings: Settings, rec: CopyRecommendation) -> None:
    copy_storage.save_recommendation(
        settings,
        {
            "wallet": rec.wallet,
            "market_slug": rec.market_slug,
            "market_title": rec.market_title,
            "outcome": rec.outcome,
            "action": rec.action,
            "trader_score": rec.trader_score,
            "max_alloc": rec.max_alloc,
            "reason": rec.reason,
            "entry_price": rec.entry_price,
            "current_price": rec.current_price,
            "price_drift": rec.price_drift,
            "slippage_est": rec.slippage_est,
        },
    )
    copy_storage.write_audit_log(
        settings,
        actor="recommendation_engine",
        action="recommendation",
        payload={
            "wallet": rec.wallet,
            "action": rec.action,
            "market_slug": rec.market_slug,
            "trader_score": rec.trader_score,
        },
    )
