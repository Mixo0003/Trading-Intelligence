"""Validated CLOB order-book snapshot and buy-side allocation metrics (V2.2.2 Checkpoint 6).

Pure parsing / math only. No recommendation decisions, delivery, Telegram, or paper writes.
All derived quote fields for an accepted event come from one validated /book snapshot.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from app.copy_intel.api_result import ApiFailure, ApiResult, ApiSuccess

logger = logging.getLogger(__name__)

# Quote freshness (seconds). Age exactly at the bound is accepted.
MAX_QUOTE_AGE_SECONDS = 30.0
# Allow small future skew vs HTTP receive time.
CLOCK_SKEW_TOLERANCE_SECONDS = 2.0
_FILL_EPS = 1e-9


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class ValidatedOrderBook:
    """One fail-closed /book snapshot with derived top-of-book metrics."""

    asset_id: str
    book_hash: str
    quote_at: float
    received_at: float
    quote_age_seconds: float
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    best_bid: float
    best_ask: float
    midpoint: float
    spread: float
    visible_ask_depth_usd: float
    visible_bid_depth_usd: float
    min_order_size: float | None


@dataclass(frozen=True)
class AllocationExecution:
    """Buy-side allocation sweep against a validated ask book.

    When ``available`` is False, ``execution_vwap`` and ``slippage`` are Unavailable
    (None). Spread remains the top-of-book ask−bid gap and is never called slippage.
    """

    book: ValidatedOrderBook
    allocation_usd: float
    best_bid: float
    best_ask: float
    midpoint: float
    spread: float
    visible_ask_depth_usd: float
    quote_age_seconds: float
    filled_shares: float | None
    execution_vwap: float | None
    slippage: float | None
    available: bool
    unavailable_code: str | None


def _failure(
    *,
    code: str,
    message: str,
    request_count: int = 1,
    status_code: int | None = None,
    retryable: bool = False,
) -> ApiFailure:
    return ApiFailure(
        code=code,
        status_code=status_code,
        retryable=retryable,
        request_count=max(1, int(request_count)),
        message=message,
    )


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def normalize_book_timestamp(
    raw: Any,
    *,
    received_at: float,
    max_quote_age_seconds: float = MAX_QUOTE_AGE_SECONDS,
) -> tuple[float, str] | ApiFailure:
    """Normalize book timestamp to epoch seconds.

    Official unit is not assumed. Accept seconds or milliseconds only when
    exactly one interpretation is consistent with ``received_at``.
    """
    max_age = float(max_quote_age_seconds)
    if not math.isfinite(max_age) or max_age <= 0:
        return _failure(code="invalid_max_quote_age", message="max quote age invalid")
    if raw is None or raw == "":
        return _failure(code="missing_timestamp", message="timestamp missing")
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        return _failure(code="invalid_timestamp", message="timestamp not parseable")
    if not math.isfinite(numeric) or numeric <= 0:
        return _failure(code="invalid_timestamp", message="timestamp not finite positive")

    as_seconds = numeric
    as_millis = numeric / 1000.0

    def _consistent(quote_at: float) -> bool:
        age = received_at - quote_at
        if age < -CLOCK_SKEW_TOLERANCE_SECONDS:
            return False
        if age > max_age:
            return False
        return True

    seconds_ok = _consistent(as_seconds)
    millis_ok = _consistent(as_millis)

    if seconds_ok and millis_ok:
        return _failure(
            code="ambiguous_timestamp",
            message="timestamp unit ambiguous under receive-time window",
        )
    if seconds_ok:
        return as_seconds, "seconds"
    if millis_ok:
        return as_millis, "milliseconds"

    # Distinguish future vs stale for diagnostics when neither unit fits.
    for candidate in (as_seconds, as_millis):
        age = received_at - candidate
        if age < -CLOCK_SKEW_TOLERANCE_SECONDS:
            return _failure(
                code="future_timestamp",
                message="timestamp beyond clock-skew tolerance",
            )
    return _failure(
        code="stale_quote",
        message="quote age exceeds max allowed",
    )


def _parse_levels(raw_levels: Any, *, side: str) -> tuple[BookLevel, ...] | ApiFailure:
    if raw_levels is None:
        return _failure(code=f"missing_{side}", message=f"{side} missing")
    if not isinstance(raw_levels, list):
        return _failure(code=f"invalid_{side}_type", message=f"{side} must be a list")
    if len(raw_levels) == 0:
        return _failure(code=f"empty_{side}", message=f"{side} empty")

    levels: list[BookLevel] = []
    for item in raw_levels:
        if not isinstance(item, dict):
            return _failure(code=f"invalid_{side}_level", message=f"{side} level not object")
        price = _finite_positive(item.get("price"))
        size = _finite_positive(item.get("size"))
        if price is None:
            return _failure(
                code=f"invalid_{side}_price",
                message=f"{side} price must be finite and positive",
            )
        if size is None:
            return _failure(
                code=f"invalid_{side}_size",
                message=f"{side} size must be finite and positive",
            )
        levels.append(BookLevel(price=price, size=size))

    # Explicit policy: normalize sort order (do not reject merely unsorted books).
    if side == "asks":
        levels.sort(key=lambda level: level.price)  # ascending
    else:
        levels.sort(key=lambda level: level.price, reverse=True)  # descending
    return tuple(levels)


def parse_validated_order_book(
    payload: Any,
    *,
    requested_asset: str,
    received_at: float | None = None,
    request_count: int = 1,
    max_quote_age_seconds: float | None = None,
) -> ApiResult[ValidatedOrderBook]:
    """Fail-closed parse of one CLOB /book JSON body into a validated snapshot."""
    received = float(time.time() if received_at is None else received_at)
    max_age = (
        MAX_QUOTE_AGE_SECONDS
        if max_quote_age_seconds is None
        else float(max_quote_age_seconds)
    )
    if not math.isfinite(max_age) or max_age <= 0:
        return _failure(
            code="invalid_max_quote_age",
            message="max quote age invalid",
            request_count=request_count,
        )
    if not isinstance(payload, dict):
        return _failure(
            code="wrong_type",
            message="expected book object",
            request_count=request_count,
        )

    asset_raw = payload.get("asset_id")
    if asset_raw is None or str(asset_raw).strip() == "":
        return _failure(
            code="missing_asset_id",
            message="asset_id missing",
            request_count=request_count,
        )
    asset_id = str(asset_raw).strip()
    requested = str(requested_asset).strip()
    if asset_id != requested:
        return _failure(
            code="asset_mismatch",
            message="requested asset does not match book asset_id",
            request_count=request_count,
        )

    book_hash = payload.get("hash")
    if book_hash is None or str(book_hash).strip() == "":
        return _failure(
            code="missing_hash",
            message="hash missing",
            request_count=request_count,
        )

    ts_result = normalize_book_timestamp(
        payload.get("timestamp"),
        received_at=received,
        max_quote_age_seconds=max_age,
    )
    if isinstance(ts_result, ApiFailure):
        return ApiFailure(
            code=ts_result.code,
            status_code=ts_result.status_code,
            retryable=ts_result.retryable,
            request_count=request_count,
            message=ts_result.message,
        )
    quote_at, _unit = ts_result
    quote_age = received - quote_at
    if quote_age < -CLOCK_SKEW_TOLERANCE_SECONDS:
        return _failure(
            code="future_timestamp",
            message="timestamp beyond clock-skew tolerance",
            request_count=request_count,
        )
    if quote_age > max_age:
        return _failure(
            code="stale_quote",
            message="quote age exceeds max allowed",
            request_count=request_count,
        )

    if "bids" not in payload:
        return _failure(code="missing_bids", message="bids missing", request_count=request_count)
    if "asks" not in payload:
        return _failure(code="missing_asks", message="asks missing", request_count=request_count)

    bids = _parse_levels(payload.get("bids"), side="bids")
    if isinstance(bids, ApiFailure):
        return ApiFailure(
            code=bids.code,
            status_code=bids.status_code,
            retryable=bids.retryable,
            request_count=request_count,
            message=bids.message,
        )
    asks = _parse_levels(payload.get("asks"), side="asks")
    if isinstance(asks, ApiFailure):
        return ApiFailure(
            code=asks.code,
            status_code=asks.status_code,
            retryable=asks.retryable,
            request_count=request_count,
            message=asks.message,
        )

    best_bid = bids[0].price
    best_ask = asks[0].price
    if not (best_bid < best_ask):
        return _failure(
            code="crossed_or_locked_book",
            message="best bid must be strictly less than best ask",
            request_count=request_count,
        )

    midpoint = (best_bid + best_ask) / 2.0
    spread = best_ask - best_bid
    visible_ask_depth_usd = sum(level.price * level.size for level in asks)
    visible_bid_depth_usd = sum(level.price * level.size for level in bids)

    min_order_raw = payload.get("min_order_size")
    min_order_size: float | None
    if min_order_raw is None or min_order_raw == "":
        min_order_size = None
    else:
        try:
            parsed_min = float(min_order_raw)
        except (TypeError, ValueError):
            return _failure(
                code="invalid_min_order_size",
                message="min_order_size not parseable",
                request_count=request_count,
            )
        if not math.isfinite(parsed_min) or parsed_min <= 0:
            return _failure(
                code="invalid_min_order_size",
                message="min_order_size must be finite and positive",
                request_count=request_count,
            )
        min_order_size = parsed_min

    book = ValidatedOrderBook(
        asset_id=asset_id,
        book_hash=str(book_hash).strip(),
        quote_at=quote_at,
        received_at=received,
        quote_age_seconds=quote_age,
        bids=bids,
        asks=asks,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        visible_ask_depth_usd=visible_ask_depth_usd,
        visible_bid_depth_usd=visible_bid_depth_usd,
        min_order_size=min_order_size,
    )
    return ApiSuccess(data=book, request_count=request_count)


def _unavailable(
    book: ValidatedOrderBook,
    *,
    allocation_usd: float,
    code: str,
) -> AllocationExecution:
    return AllocationExecution(
        book=book,
        allocation_usd=allocation_usd,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        midpoint=book.midpoint,
        spread=book.spread,
        visible_ask_depth_usd=book.visible_ask_depth_usd,
        quote_age_seconds=book.quote_age_seconds,
        filled_shares=None,
        execution_vwap=None,
        slippage=None,
        available=False,
        unavailable_code=code,
    )


def allocate_buy(
    book: ValidatedOrderBook,
    allocation_usd: float,
) -> AllocationExecution:
    """Sweep asks for a proposed USD allocation; fail closed on partial fill."""
    if not math.isfinite(allocation_usd) or allocation_usd <= 0:
        return _unavailable(
            book, allocation_usd=allocation_usd, code="invalid_allocation"
        )
    if allocation_usd > book.visible_ask_depth_usd + _FILL_EPS:
        return _unavailable(
            book, allocation_usd=allocation_usd, code="insufficient_depth"
        )

    remaining = float(allocation_usd)
    filled_shares = 0.0
    for level in book.asks:
        level_usd = level.price * level.size
        spend = min(remaining, level_usd)
        if spend <= 0:
            break
        filled_shares += spend / level.price
        remaining -= spend
        if remaining <= _FILL_EPS:
            remaining = 0.0
            break

    if remaining > _FILL_EPS:
        # Should not happen when allocation <= depth, but never accept partial.
        return _unavailable(
            book, allocation_usd=allocation_usd, code="partial_fill"
        )
    if filled_shares <= 0 or not math.isfinite(filled_shares):
        return _unavailable(
            book, allocation_usd=allocation_usd, code="invalid_fill"
        )

    if book.min_order_size is not None and filled_shares + _FILL_EPS < book.min_order_size:
        return _unavailable(
            book, allocation_usd=allocation_usd, code="below_min_order_size"
        )

    execution_vwap = allocation_usd / filled_shares
    if book.midpoint <= 0 or not math.isfinite(book.midpoint):
        return _unavailable(
            book, allocation_usd=allocation_usd, code="invalid_midpoint"
        )
    slippage = (execution_vwap - book.midpoint) / book.midpoint
    return AllocationExecution(
        book=book,
        allocation_usd=allocation_usd,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        midpoint=book.midpoint,
        spread=book.spread,
        visible_ask_depth_usd=book.visible_ask_depth_usd,
        quote_age_seconds=book.quote_age_seconds,
        filled_shares=filled_shares,
        execution_vwap=execution_vwap,
        slippage=slippage,
        available=True,
        unavailable_code=None,
    )


def sanitized_book_failure(failure: ApiFailure) -> dict[str, Any]:
    """Sanitized diagnostic dict (no response bodies or absolute paths)."""
    return {
        "code": failure.code,
        "status_code": failure.status_code,
        "retryable": failure.retryable,
        "request_count": failure.request_count,
        "message": failure.message,
    }


__all__ = [
    "AllocationExecution",
    "BookLevel",
    "CLOCK_SKEW_TOLERANCE_SECONDS",
    "MAX_QUOTE_AGE_SECONDS",
    "ValidatedOrderBook",
    "allocate_buy",
    "normalize_book_timestamp",
    "parse_validated_order_book",
    "sanitized_book_failure",
]
