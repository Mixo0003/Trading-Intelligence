"""Spread Monitor pure logic — top-of-book spread percentage."""

from __future__ import annotations

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.freshness import require_fresh_timestamp
from app.crypto.models import Instrument, SpreadQuote, Ticker


def compute_spread_pct(best_bid: float, best_ask: float) -> ApiSuccess[float] | ApiFailure:
    if best_bid <= 0 or best_ask <= 0:
        return ApiFailure(
            code="invalid_quote",
            status_code=None,
            retryable=False,
            request_count=0,
            message="Bid/ask must be positive",
        )
    if best_ask < best_bid:
        return ApiFailure(
            code="crossed_book",
            status_code=None,
            retryable=False,
            request_count=0,
            message="Ask is below bid",
        )
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return ApiFailure(
            code="invalid_quote",
            status_code=None,
            retryable=False,
            request_count=0,
            message="Mid price invalid",
        )
    return ApiSuccess(data=((best_ask - best_bid) / mid) * 100.0)


def build_spread_quote(
    ticker: Ticker,
    *,
    max_age_seconds: float,
    now_ms: int | None = None,
) -> ApiSuccess[SpreadQuote] | ApiFailure:
    fresh = require_fresh_timestamp(
        ticker.timestamp_ms,
        max_age_seconds=max_age_seconds,
        now=now_ms,
    )
    if isinstance(fresh, ApiFailure):
        return fresh
    _ts, age = fresh.data
    spread = compute_spread_pct(ticker.best_bid, ticker.best_ask)
    if isinstance(spread, ApiFailure):
        return spread
    return ApiSuccess(
        data=SpreadQuote(
            exchange_id=ticker.exchange_id,
            instrument=ticker.instrument,
            best_bid=ticker.best_bid,
            best_ask=ticker.best_ask,
            spread_pct=spread.data,
            timestamp_ms=ticker.timestamp_ms,
            age_seconds=age,
            freshness="fresh",
        )
    )


def validate_instrument_allowlist(
    symbol: str,
    allowed: list[str],
) -> ApiSuccess[Instrument] | ApiFailure:
    instrument = Instrument.from_canonical(symbol, market_type="spot")
    if instrument is None:
        return ApiFailure(
            code="unsupported_market_type",
            status_code=None,
            retryable=False,
            request_count=0,
            message=f"Symbol/market type unsupported: {symbol}",
        )
    allowed_norm: set[str] = set()
    for item in allowed:
        parsed = Instrument.from_canonical(item)
        if parsed is not None:
            allowed_norm.add(parsed.canonical_symbol)
    if instrument.canonical_symbol not in allowed_norm:
        return ApiFailure(
            code="symbol_mismatch",
            status_code=None,
            retryable=False,
            request_count=0,
            message=f"Symbol not allowlisted: {instrument.canonical_symbol}",
        )
    return ApiSuccess(data=instrument)
