"""CEX L2 order-book normalization and allocation (public read-only)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.freshness import now_ms, require_fresh_timestamp
from app.crypto.models import BookLevel, Instrument, OrderBookSnapshot

_FILL_EPS = 1e-9
DEFAULT_BOOK_LIMIT = 20


@dataclass(frozen=True)
class BookAllocation:
    """Depth sweep result for one side of the book."""

    available: bool
    unavailable_code: str | None
    allocation_usd: float
    filled_base: float | None
    execution_vwap: float | None
    proceeds_usd: float | None
    slippage: float | None
    midpoint: float | None
    best_bid: float | None
    best_ask: float | None
    visible_depth_usd: float


def _failure(
    *,
    code: str,
    message: str,
    request_count: int = 1,
) -> ApiFailure:
    return ApiFailure(
        code=code,
        status_code=None,
        retryable=False,
        request_count=max(1, int(request_count)),
        message=message,
    )


def _parse_level(raw: Any) -> BookLevel | None:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            price = float(raw[0])
            size = float(raw[1])
        except (TypeError, ValueError):
            return None
    elif isinstance(raw, dict):
        try:
            price = float(raw.get("price") or raw.get("px") or raw.get(0))
            size = float(
                raw.get("size")
                or raw.get("qty")
                or raw.get("sz")
                or raw.get("quantity")
                or raw.get(1)
            )
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not math.isfinite(price) or not math.isfinite(size):
        return None
    if price <= 0 or size <= 0:
        return None
    return BookLevel(price=price, size=size)


def parse_book_levels(raw_levels: Any) -> tuple[BookLevel, ...] | None:
    if not isinstance(raw_levels, list) or not raw_levels:
        return None
    levels: list[BookLevel] = []
    for row in raw_levels:
        level = _parse_level(row)
        if level is None:
            return None
        levels.append(level)
    return tuple(levels)


def build_order_book_snapshot(
    *,
    exchange_id: str,
    instrument: Instrument,
    bids_raw: Any,
    asks_raw: Any,
    timestamp_ms: Any,
    request_count: int = 1,
    allow_receive_time: bool = False,
    max_age_seconds: float | None = None,
) -> ApiSuccess[OrderBookSnapshot] | ApiFailure:
    bids = parse_book_levels(bids_raw)
    asks = parse_book_levels(asks_raw)
    if bids is None or asks is None:
        return _failure(
            code="malformed_order_book",
            message="Order book levels missing or malformed",
            request_count=request_count,
        )
    # Bids descending, asks ascending for consistent sweeps.
    bids_sorted = tuple(sorted(bids, key=lambda level: level.price, reverse=True))
    asks_sorted = tuple(sorted(asks, key=lambda level: level.price))
    if asks_sorted[0].price < bids_sorted[0].price:
        return _failure(
            code="crossed_book",
            message="Ask below bid",
            request_count=request_count,
        )
    from app.crypto.exchanges.base import parse_timestamp_ms

    ts = parse_timestamp_ms(timestamp_ms, fallback_now=allow_receive_time)
    if ts is None:
        return _failure(
            code="missing_timestamp",
            message="Order book timestamp missing",
            request_count=request_count,
        )
    if max_age_seconds is not None:
        fresh = require_fresh_timestamp(ts, max_age_seconds=max_age_seconds)
        if isinstance(fresh, ApiFailure):
            return ApiFailure(
                code=fresh.code,
                status_code=fresh.status_code,
                retryable=fresh.retryable,
                request_count=request_count,
                message=fresh.message,
            )
    return ApiSuccess(
        data=OrderBookSnapshot(
            exchange_id=exchange_id,
            instrument=instrument,
            bids=bids_sorted,
            asks=asks_sorted,
            timestamp_ms=ts,
        ),
        request_count=request_count,
    )


def book_midpoint(book: OrderBookSnapshot) -> float | None:
    if not book.bids or not book.asks:
        return None
    mid = (book.bids[0].price + book.asks[0].price) / 2.0
    if not math.isfinite(mid) or mid <= 0:
        return None
    return mid


def visible_ask_depth_usd(book: OrderBookSnapshot) -> float:
    return sum(level.price * level.size for level in book.asks)


def visible_bid_depth_usd(book: OrderBookSnapshot) -> float:
    return sum(level.price * level.size for level in book.bids)


def allocate_buy_usd(book: OrderBookSnapshot, allocation_usd: float) -> BookAllocation:
    """Sweep asks until ``allocation_usd`` is spent; fail closed on partial fill."""
    mid = book_midpoint(book)
    best_bid = book.bids[0].price if book.bids else None
    best_ask = book.asks[0].price if book.asks else None
    depth = visible_ask_depth_usd(book)
    base = BookAllocation(
        available=False,
        unavailable_code=None,
        allocation_usd=float(allocation_usd),
        filled_base=None,
        execution_vwap=None,
        proceeds_usd=None,
        slippage=None,
        midpoint=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        visible_depth_usd=depth,
    )
    if not math.isfinite(allocation_usd) or allocation_usd <= 0:
        return BookAllocation(**{**base.__dict__, "unavailable_code": "invalid_allocation"})
    if mid is None:
        return BookAllocation(**{**base.__dict__, "unavailable_code": "invalid_midpoint"})
    if allocation_usd > depth + _FILL_EPS:
        return BookAllocation(**{**base.__dict__, "unavailable_code": "insufficient_depth"})

    remaining = float(allocation_usd)
    filled_base = 0.0
    for level in book.asks:
        level_usd = level.price * level.size
        spend = min(remaining, level_usd)
        if spend <= 0:
            break
        filled_base += spend / level.price
        remaining -= spend
        if remaining <= _FILL_EPS:
            remaining = 0.0
            break
    if remaining > _FILL_EPS or filled_base <= 0:
        return BookAllocation(**{**base.__dict__, "unavailable_code": "partial_fill"})
    vwap = allocation_usd / filled_base
    slippage = (vwap - mid) / mid
    return BookAllocation(
        available=True,
        unavailable_code=None,
        allocation_usd=float(allocation_usd),
        filled_base=filled_base,
        execution_vwap=vwap,
        proceeds_usd=float(allocation_usd),
        slippage=slippage,
        midpoint=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        visible_depth_usd=depth,
    )


def allocate_sell_base(book: OrderBookSnapshot, base_qty: float) -> BookAllocation:
    """Sweep bids to sell ``base_qty``; fail closed on partial fill."""
    mid = book_midpoint(book)
    best_bid = book.bids[0].price if book.bids else None
    best_ask = book.asks[0].price if book.asks else None
    depth = visible_bid_depth_usd(book)
    base = BookAllocation(
        available=False,
        unavailable_code=None,
        allocation_usd=0.0,
        filled_base=None,
        execution_vwap=None,
        proceeds_usd=None,
        slippage=None,
        midpoint=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        visible_depth_usd=depth,
    )
    if not math.isfinite(base_qty) or base_qty <= 0:
        return BookAllocation(**{**base.__dict__, "unavailable_code": "invalid_allocation"})
    if mid is None:
        return BookAllocation(**{**base.__dict__, "unavailable_code": "invalid_midpoint"})

    remaining = float(base_qty)
    proceeds = 0.0
    for level in book.bids:
        take = min(remaining, level.size)
        if take <= 0:
            break
        proceeds += take * level.price
        remaining -= take
        if remaining <= _FILL_EPS:
            remaining = 0.0
            break
    if remaining > _FILL_EPS or proceeds <= 0:
        return BookAllocation(**{**base.__dict__, "unavailable_code": "insufficient_depth"})
    vwap = proceeds / base_qty
    slippage = (mid - vwap) / mid
    return BookAllocation(
        available=True,
        unavailable_code=None,
        allocation_usd=proceeds,
        filled_base=float(base_qty),
        execution_vwap=vwap,
        proceeds_usd=proceeds,
        slippage=slippage,
        midpoint=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        visible_depth_usd=depth,
    )


def age_seconds(book: OrderBookSnapshot, *, receive_ms: int | None = None) -> float:
    receive = receive_ms if receive_ms is not None else now_ms()
    return max(0.0, (receive - book.timestamp_ms) / 1000.0)
