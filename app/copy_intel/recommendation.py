"""COPY / WATCH / SKIP recommendation engine (V2.2.2 Checkpoint 7).

Uses authoritative Checkpoint 5 trade correlation and one validated Checkpoint 6
/book snapshot. Does not write paper positions or deliver Telegram alerts.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from app.config.settings import Settings
from app.copy_intel.analytics import compute_trader_metrics
from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.models import AlertDecisions, CopyAction, CopyRecommendation, TradeEvent
from app.copy_intel.order_book import (
    MAX_QUOTE_AGE_SECONDS,
    AllocationExecution,
    ValidatedOrderBook,
    allocate_buy,
)
from app.copy_intel.risk import assert_read_only, check_exposure_limits
from app.copy_intel.scoring import score_trader
from app.database import copy_storage

# Initial book fetch + at most one freshness refetch.
MAX_BOOK_REQUESTS_PER_EVALUATION = 2
_BASE_ALLOC_FRACTION = 0.03
_COPY_SCORE_THRESHOLD = 55.0

# Stable sanitized reason codes.
REASON_AUTHORITATIVE_EVENT_REQUIRED = "authoritative_event_required"
REASON_UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
REASON_EVENT_TOO_OLD = "event_too_old"
REASON_QUOTE_UNAVAILABLE = "quote_unavailable"
REASON_QUOTE_STALE = "quote_stale"
REASON_INSUFFICIENT_DEPTH = "insufficient_depth"
REASON_BELOW_MIN_LIQUIDITY = "below_min_liquidity"
REASON_ALLOCATION_UNAVAILABLE = "allocation_unavailable"
REASON_SLIPPAGE_UNAVAILABLE = "slippage_unavailable"
REASON_SLIPPAGE_LIMIT_EXCEEDED = "slippage_limit_exceeded"
REASON_EXPOSURE_CAPACITY_ZERO = "exposure_capacity_zero"
REASON_METRICS_INCOMPLETE = "metrics_incomplete"
REASON_SCORE_BELOW_THRESHOLD = "score_below_threshold"
REASON_COPY_ELIGIBLE = "copy_eligible"
REASON_PRICE_DRIFT_LIMIT_EXCEEDED = "price_drift_limit_exceeded"
REASON_NO_EXECUTABLE_ASK = "no_executable_ask"
REASON_REQUEST_BUDGET_EXCEEDED = "request_budget_exceeded"
REASON_ASSET_MISSING = "asset_missing"


def is_recommendation_deliverable(
    alert_decisions: AlertDecisions | str, action: CopyAction | str
) -> bool:
    """Pure delivery eligibility (no scheduler / Telegram side effects).

    FOLLOW_ONLY: COPY only.
    FOLLOW_AND_WATCH: COPY and WATCH.
    SKIP is never deliverable.
    """
    if action == "SKIP":
        return False
    if action == "COPY":
        return True
    if action == "WATCH":
        return alert_decisions == "FOLLOW_AND_WATCH"
    return False


def build_recommendation(
    settings: Settings,
    event: TradeEvent,
    *,
    trader_label: str | None = None,
    client: CopyDataClient | None = None,
    min_liquidity_usd: float | None = None,
    max_event_age_seconds: int | None = None,
    now: float | None = None,
) -> CopyRecommendation:
    """Evaluate one accepted New/Increase event into COPY / WATCH / SKIP.

    Does not open paper positions. WATCH/SKIP envelopes persist here idempotently.
    COPY envelopes are deferred to the atomic Checkpoint 8 paper-commit path.
    """
    assert_read_only(settings)
    client = client or CopyDataClient(settings)
    now_ts = float(time.time() if now is None else now)
    label = trader_label or event.wallet[:10]
    liquidity_floor = (
        float(min_liquidity_usd)
        if min_liquidity_usd is not None
        else float(settings.copy_min_liquidity_usd)
    )
    bankroll = float(settings.copy_paper_bankroll_usd)
    max_age = _effective_max_age_seconds(
        settings, owner_max_event_age_seconds=max_event_age_seconds
    )

    hard = _hard_gate_authoritative_event(event, now_ts=now_ts, max_age=max_age)
    if hard is not None:
        return _finalize(
            settings,
            event=event,
            label=label,
            action="SKIP",
            reason_codes=hard,
            trader_score=0.0,
            bankroll=bankroll,
            now_ts=now_ts,
        )

    book_requests = 0
    book, book_requests, book_fail = _fetch_book(
        client, event.asset or "", request_budget=MAX_BOOK_REQUESTS_PER_EVALUATION
    )
    if book_fail is not None or book is None:
        return _finalize(
            settings,
            event=event,
            label=label,
            action="SKIP",
            reason_codes=(book_fail or REASON_QUOTE_UNAVAILABLE,),
            trader_score=0.0,
            bankroll=bankroll,
            now_ts=now_ts,
        )

    base_alloc = bankroll * _BASE_ALLOC_FRACTION
    exposure_ok, exposure_reason, capped = check_exposure_limits(
        settings,
        wallet=event.wallet,
        condition_id=event.condition_id or "",
        proposed_notional=base_alloc,
    )
    if not exposure_ok or capped <= 0 or not math.isfinite(capped):
        return _finalize(
            settings,
            event=event,
            label=label,
            action="SKIP",
            reason_codes=(REASON_EXPOSURE_CAPACITY_ZERO,),
            trader_score=0.0,
            bankroll=bankroll,
            now_ts=now_ts,
            book=book,
            reason_detail=exposure_reason,
        )

    proposed = float(capped)
    if proposed <= 0 or not math.isfinite(proposed):
        return _finalize(
            settings,
            event=event,
            label=label,
            action="SKIP",
            reason_codes=(REASON_ALLOCATION_UNAVAILABLE,),
            trader_score=0.0,
            bankroll=bankroll,
            now_ts=now_ts,
            book=book,
        )

    metrics = compute_trader_metrics(settings, event.wallet, client=client)
    trader_score = float(score_trader(metrics))

    # Soft/hard evaluation against the current book; may refetch once if stale.
    decision = _decide_from_book(
        settings,
        event=event,
        book=book,
        proposed=proposed,
        bankroll=bankroll,
        liquidity_floor=liquidity_floor,
        trader_score=trader_score,
        metrics_eligible=bool(metrics.eligible),
        metrics_reason=metrics.ineligible_reason,
        now_ts=now_ts,
    )

    if decision.action in {"COPY", "WATCH"}:
        age_now = now_ts - book.quote_at
        if age_now > MAX_QUOTE_AGE_SECONDS:
            if book_requests >= MAX_BOOK_REQUESTS_PER_EVALUATION:
                return _finalize(
                    settings,
                    event=event,
                    label=label,
                    action="SKIP",
                    reason_codes=(REASON_REQUEST_BUDGET_EXCEEDED,),
                    trader_score=trader_score,
                    bankroll=bankroll,
                    now_ts=now_ts,
                    book=book,
                    proposed=proposed,
                )
            fresh, book_requests, refetch_fail = _fetch_book(
                client,
                event.asset or "",
                request_budget=MAX_BOOK_REQUESTS_PER_EVALUATION,
                already_used=book_requests,
            )
            if refetch_fail is not None or fresh is None:
                return _finalize(
                    settings,
                    event=event,
                    label=label,
                    action="SKIP",
                    reason_codes=(refetch_fail or REASON_QUOTE_UNAVAILABLE,),
                    trader_score=trader_score,
                    bankroll=bankroll,
                    now_ts=now_ts,
                    proposed=proposed,
                )
            # Full replacement — recompute every derived field from the new snapshot.
            book = fresh
            decision = _decide_from_book(
                settings,
                event=event,
                book=book,
                proposed=proposed,
                bankroll=bankroll,
                liquidity_floor=liquidity_floor,
                trader_score=trader_score,
                metrics_eligible=bool(metrics.eligible),
                metrics_reason=metrics.ineligible_reason,
                now_ts=now_ts,
            )
            if decision.action in {"COPY", "WATCH"}:
                if (now_ts - book.quote_at) > MAX_QUOTE_AGE_SECONDS:
                    return _finalize(
                        settings,
                        event=event,
                        label=label,
                        action="SKIP",
                        reason_codes=(REASON_QUOTE_STALE,),
                        trader_score=trader_score,
                        bankroll=bankroll,
                        now_ts=now_ts,
                        book=book,
                        proposed=proposed,
                        execution=decision.execution,
                    )

    return _finalize(
        settings,
        event=event,
        label=label,
        action=decision.action,
        reason_codes=decision.reason_codes,
        trader_score=trader_score,
        bankroll=bankroll,
        now_ts=now_ts,
        book=book,
        proposed=proposed,
        execution=decision.execution,
        price_drift=decision.price_drift,
        reason_detail=decision.reason_detail,
    )


class _Decision:
    __slots__ = ("action", "reason_codes", "execution", "price_drift", "reason_detail")

    def __init__(
        self,
        action: CopyAction,
        reason_codes: tuple[str, ...],
        *,
        execution: AllocationExecution | None = None,
        price_drift: float | None = None,
        reason_detail: str | None = None,
    ) -> None:
        self.action = action
        self.reason_codes = reason_codes
        self.execution = execution
        self.price_drift = price_drift
        self.reason_detail = reason_detail


def _decide_from_book(
    settings: Settings,
    *,
    event: TradeEvent,
    book: ValidatedOrderBook,
    proposed: float,
    bankroll: float,
    liquidity_floor: float,
    trader_score: float,
    metrics_eligible: bool,
    metrics_reason: str | None,
    now_ts: float,
) -> _Decision:
    if not math.isfinite(book.best_ask) or book.best_ask <= 0:
        return _Decision("SKIP", (REASON_NO_EXECUTABLE_ASK,))
    if book.visible_ask_depth_usd + 1e-12 < liquidity_floor:
        return _Decision("SKIP", (REASON_BELOW_MIN_LIQUIDITY,))

    execution = allocate_buy(book, proposed)
    if not execution.available:
        code = execution.unavailable_code or REASON_ALLOCATION_UNAVAILABLE
        if code == "insufficient_depth":
            return _Decision("SKIP", (REASON_INSUFFICIENT_DEPTH,), execution=execution)
        if code in {"invalid_allocation", "below_min_order_size", "partial_fill", "invalid_fill"}:
            return _Decision("SKIP", (REASON_ALLOCATION_UNAVAILABLE,), execution=execution)
        if code in {"invalid_midpoint"} or execution.slippage is None:
            return _Decision("SKIP", (REASON_SLIPPAGE_UNAVAILABLE,), execution=execution)
        return _Decision("SKIP", (REASON_ALLOCATION_UNAVAILABLE,), execution=execution)

    if execution.execution_vwap is None or not math.isfinite(execution.execution_vwap):
        return _Decision("SKIP", (REASON_ALLOCATION_UNAVAILABLE,), execution=execution)
    if execution.slippage is None or not math.isfinite(execution.slippage):
        return _Decision("SKIP", (REASON_SLIPPAGE_UNAVAILABLE,), execution=execution)

    trader_vwap = float(event.trader_vwap or 0.0)
    price_drift: float | None = None
    if trader_vwap > 0 and math.isfinite(trader_vwap):
        price_drift = (book.midpoint - trader_vwap) / trader_vwap

    soft_codes: list[str] = []
    if execution.slippage > settings.copy_max_slippage_pct:
        soft_codes.append(REASON_SLIPPAGE_LIMIT_EXCEEDED)
    if (
        price_drift is not None
        and math.isfinite(price_drift)
        and abs(price_drift) > settings.copy_max_slippage_pct
    ):
        soft_codes.append(REASON_PRICE_DRIFT_LIMIT_EXCEEDED)
    if not metrics_eligible:
        soft_codes.append(REASON_METRICS_INCOMPLETE)
    if trader_score < _COPY_SCORE_THRESHOLD:
        soft_codes.append(REASON_SCORE_BELOW_THRESHOLD)

    if soft_codes:
        detail = metrics_reason if REASON_METRICS_INCOMPLETE in soft_codes else None
        return _Decision(
            "WATCH",
            tuple(soft_codes),
            execution=execution,
            price_drift=price_drift,
            reason_detail=detail,
        )
    return _Decision(
        "COPY",
        (REASON_COPY_ELIGIBLE,),
        execution=execution,
        price_drift=price_drift,
    )


def _fetch_book(
    client: CopyDataClient,
    asset: str,
    *,
    request_budget: int,
    already_used: int = 0,
) -> tuple[ValidatedOrderBook | None, int, str | None]:
    used = already_used
    if not asset:
        return None, used, REASON_ASSET_MISSING
    if used >= request_budget:
        return None, used, REASON_REQUEST_BUDGET_EXCEEDED
    used += 1
    result = client.fetch_validated_order_book(asset)
    if isinstance(result, ApiFailure):
        code = result.code
        if code in {
            "stale_quote",
            "future_timestamp",
            "ambiguous_timestamp",
            "missing_timestamp",
            "invalid_timestamp",
        }:
            return None, used, REASON_QUOTE_STALE if code == "stale_quote" else REASON_QUOTE_UNAVAILABLE
        if code == "asset_mismatch":
            return None, used, REASON_QUOTE_UNAVAILABLE
        return None, used, REASON_QUOTE_UNAVAILABLE
    if not isinstance(result, ApiSuccess):
        return None, used, REASON_QUOTE_UNAVAILABLE
    return result.data, used, None


def _hard_gate_authoritative_event(
    event: TradeEvent, *, now_ts: float, max_age: int
) -> tuple[str, ...] | None:
    if event.event_type not in {"open", "increase"}:
        return (REASON_UNSUPPORTED_EVENT_TYPE,)
    if event.trader_vwap is None or not math.isfinite(float(event.trader_vwap)):
        return (REASON_AUTHORITATIVE_EVENT_REQUIRED,)
    if event.first_fill_ts is None or event.last_fill_ts is None:
        return (REASON_AUTHORITATIVE_EVENT_REQUIRED,)
    if event.asset is None or str(event.asset).strip() == "":
        return (REASON_ASSET_MISSING,)
    event_age = now_ts - float(event.last_fill_ts)
    if event_age > float(max_age):
        return (REASON_EVENT_TOO_OLD,)
    return None


def _effective_max_age_seconds(
    settings: Settings, *, owner_max_event_age_seconds: int | None
) -> int:
    candidates = [
        int(settings.copy_max_event_age_seconds),
        int(settings.copy_max_trade_age_min) * 60,
    ]
    if owner_max_event_age_seconds is not None:
        candidates.append(int(owner_max_event_age_seconds))
    return max(1, min(candidates))


def _finalize(
    settings: Settings,
    *,
    event: TradeEvent,
    label: str,
    action: CopyAction,
    reason_codes: tuple[str, ...],
    trader_score: float,
    bankroll: float,
    now_ts: float,
    book: ValidatedOrderBook | None = None,
    proposed: float | None = None,
    execution: AllocationExecution | None = None,
    price_drift: float | None = None,
    reason_detail: str | None = None,
) -> CopyRecommendation:
    event_age = (
        now_ts - float(event.last_fill_ts)
        if event.last_fill_ts is not None
        else None
    )
    share = None
    if proposed is not None and bankroll > 0 and math.isfinite(proposed):
        share = proposed / bankroll

    slippage = execution.slippage if execution is not None else None
    execution_vwap = execution.execution_vwap if execution is not None else None
    filled = execution.filled_shares if execution is not None else None

    reason = _format_reason(reason_codes, detail=reason_detail)
    rec = CopyRecommendation(
        wallet=event.wallet,
        trader_label=label,
        market_title=event.title or "Unknown market",
        market_slug=event.slug,
        outcome=event.outcome,
        event_type=event.event_type,
        entry_price=event.trader_vwap,
        current_price=book.midpoint if book is not None else None,
        price_drift=price_drift,
        slippage_est=slippage,
        trader_score=trader_score,
        max_alloc=float(proposed or 0.0),
        action=action,
        reason=reason,
        event_uid=event.event_uid,
        first_fill_ts=event.first_fill_ts,
        last_fill_ts=event.last_fill_ts,
        detected_at=event.detected_at,
        event_age_seconds=event_age,
        trader_vwap=event.trader_vwap,
        midpoint=book.midpoint if book is not None else None,
        best_bid=book.best_bid if book is not None else None,
        best_ask=book.best_ask if book is not None else None,
        execution_vwap=execution_vwap,
        slippage=slippage,
        visible_ask_depth_usd=(
            book.visible_ask_depth_usd if book is not None else None
        ),
        proposed_allocation_usd=proposed,
        paper_bankroll_usd=bankroll,
        paper_bankroll_share=share,
        resulting_position_size=filled,
        size_delta=event.size,
        matched_fill_count=event.matched_fill_count,
        quote_at=book.quote_at if book is not None else None,
        quote_age_seconds=book.quote_age_seconds if book is not None else None,
        reason_codes=reason_codes,
        spread=book.spread if book is not None else None,
    )
    _persist_recommendation(settings, rec)
    return rec


def _format_reason(codes: tuple[str, ...], *, detail: str | None = None) -> str:
    if not codes:
        return "unevaluated"
    text = "; ".join(codes)
    if detail:
        return f"{text}: {detail}"
    return text


def _persist_recommendation(settings: Settings, rec: CopyRecommendation) -> None:
    # Checkpoint 8: COPY recommendation + paper must share one transaction.
    if rec.action == "COPY":
        return
    envelope = {
        "event_uid": rec.event_uid,
        "wallet": rec.wallet,
        "action": rec.action,
        "reason_codes": list(rec.reason_codes),
        "event_type": rec.event_type,
        "first_fill_ts": rec.first_fill_ts,
        "last_fill_ts": rec.last_fill_ts,
        "detected_at": rec.detected_at,
        "event_age_seconds": rec.event_age_seconds,
        "trader_vwap": rec.trader_vwap,
        "midpoint": rec.midpoint,
        "best_bid": rec.best_bid,
        "best_ask": rec.best_ask,
        "execution_vwap": rec.execution_vwap,
        "slippage": rec.slippage,
        "spread": rec.spread,
        "visible_ask_depth_usd": rec.visible_ask_depth_usd,
        "proposed_allocation_usd": rec.proposed_allocation_usd,
        "paper_bankroll_usd": rec.paper_bankroll_usd,
        "paper_bankroll_share": rec.paper_bankroll_share,
        "resulting_position_size": rec.resulting_position_size,
        "size_delta": rec.size_delta,
        "matched_fill_count": rec.matched_fill_count,
        "quote_at": rec.quote_at,
        "quote_age_seconds": rec.quote_age_seconds,
        "trader_score": rec.trader_score,
        "price_drift": rec.price_drift,
    }
    copy_storage.save_recommendation_envelope(
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
            "event_uid": rec.event_uid,
            "envelope_json": json.dumps(envelope, separators=(",", ":"), sort_keys=True),
            "reason_codes_json": json.dumps(list(rec.reason_codes)),
        },
    )
    copy_storage.write_audit_log(
        settings,
        actor="recommendation_engine",
        action="recommendation",
        payload={
            "wallet": rec.wallet,
            "action": rec.action,
            "event_uid": rec.event_uid,
            "reason_codes": list(rec.reason_codes),
            "market_slug": rec.market_slug,
            "trader_score": rec.trader_score,
        },
    )


# Keep legacy helper names imported by older tests only if still referenced.
# estimate_slippage is intentionally not used by the V2.2.2 path.
