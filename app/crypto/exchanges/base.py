"""Shared public REST helpers for Crypto Intelligence exchange adapters."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import requests

from app.config.settings import Settings
from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.freshness import now_ms
from app.crypto.models import (
    ExchangeHealth,
    HealthStatus,
    Instrument,
    OrderBookSnapshot,
    Ticker,
)
from app.crypto.risk import assert_crypto_read_only
from app.http.retry import request_with_retry

logger = logging.getLogger(__name__)


class PublicMarketDataClient(Protocol):
    exchange_id: str
    display_name: str

    def fetch_ticker(
        self, instrument: Instrument
    ) -> ApiSuccess[Ticker] | ApiFailure: ...

    def probe_health(self) -> ExchangeHealth: ...


class PublicOrderBookClient(Protocol):
    """V4.3 venues that expose public L2 depth."""

    exchange_id: str
    display_name: str

    def fetch_ticker(
        self, instrument: Instrument
    ) -> ApiSuccess[Ticker] | ApiFailure: ...

    def fetch_order_book(
        self,
        instrument: Instrument,
        *,
        limit: int = 20,
    ) -> ApiSuccess[OrderBookSnapshot] | ApiFailure: ...

    def probe_health(self) -> ExchangeHealth: ...


def venue_symbol_compact(instrument: Instrument) -> str:
    return f"{instrument.base}{instrument.quote}"


def venue_symbol_dash(instrument: Instrument) -> str:
    return f"{instrument.base}-{instrument.quote}"


def venue_symbol_underscore(instrument: Instrument) -> str:
    return f"{instrument.base}_{instrument.quote}"


def public_get_json(
    settings: Settings,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_attempts: int = 2,
) -> ApiSuccess[Any] | ApiFailure:
    """GET JSON from a public endpoint. Never attaches auth headers."""
    assert_crypto_read_only(settings)
    timeout = settings.crypto_http_timeout_seconds
    try:
        response = request_with_retry(
            "GET",
            url,
            params=params or {},
            timeout=timeout,
            max_attempts=max_attempts,
            headers={"Accept": "application/json", "User-Agent": "TradingIntelligence/3.0"},
        )
    except (requests.RequestException, OSError) as exc:
        logger.warning("Crypto public GET failed for %s: %s", url.split("?")[0], type(exc).__name__)
        return ApiFailure(
            code="http_error",
            status_code=None,
            retryable=True,
            request_count=max_attempts,
            message="Public market-data request failed",
        )
    count = int(getattr(response, "request_count", 1) or 1)
    if response.status_code != 200:
        return ApiFailure(
            code="http_status",
            status_code=response.status_code,
            retryable=response.status_code in {429, 500, 502, 503, 504},
            request_count=count,
            message=f"HTTP {response.status_code}",
        )
    try:
        payload = response.json()
    except ValueError:
        return ApiFailure(
            code="invalid_json",
            status_code=response.status_code,
            retryable=False,
            request_count=count,
            message="Response was not JSON",
        )
    return ApiSuccess(data=payload, request_count=count)


def parse_positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number != number:  # NaN check
        return None
    return number


def parse_timestamp_ms(value: Any, *, fallback_now: bool = False) -> int | None:
    if value is None or value == "":
        return now_ms() if fallback_now else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return now_ms() if fallback_now else None
    if number <= 0:
        return now_ms() if fallback_now else None
    # Seconds vs milliseconds heuristic
    if number < 1_000_000_000_000:
        number *= 1000.0
    return int(number)


def build_ticker(
    *,
    exchange_id: str,
    instrument: Instrument,
    best_bid: Any,
    best_ask: Any,
    timestamp_ms: Any,
    request_count: int = 1,
    allow_receive_time: bool = False,
) -> ApiSuccess[Ticker] | ApiFailure:
    bid = parse_positive_float(best_bid)
    ask = parse_positive_float(best_ask)
    if bid is None or ask is None:
        return ApiFailure(
            code="invalid_quote",
            status_code=None,
            retryable=False,
            request_count=request_count,
            message="Missing or invalid bid/ask",
        )
    if ask < bid:
        return ApiFailure(
            code="crossed_book",
            status_code=None,
            retryable=False,
            request_count=request_count,
            message="Ask below bid",
        )
    ts = parse_timestamp_ms(timestamp_ms, fallback_now=allow_receive_time)
    if ts is None:
        return ApiFailure(
            code="missing_timestamp",
            status_code=None,
            retryable=False,
            request_count=request_count,
            message="Timestamp missing",
        )
    return ApiSuccess(
        data=Ticker(
            exchange_id=exchange_id,
            instrument=instrument,
            best_bid=bid,
            best_ask=ask,
            timestamp_ms=ts,
        ),
        request_count=request_count,
    )


def health_from_probe(
    exchange_id: str,
    result: ApiSuccess[Any] | ApiFailure,
    *,
    started_ms: int,
) -> ExchangeHealth:
    latency = max(0.0, float(now_ms() - started_ms))
    checked = now_ms()
    if isinstance(result, ApiFailure):
        status = HealthStatus.DEGRADED if result.retryable else HealthStatus.DOWN
        if result.code in {"unsupported", "unsupported_market_type"}:
            status = HealthStatus.UNSUPPORTED
        return ExchangeHealth(
            exchange_id=exchange_id,
            status=status,
            detail=result.message,
            checked_at_ms=checked,
            latency_ms=latency,
        )
    return ExchangeHealth(
        exchange_id=exchange_id,
        status=HealthStatus.OK,
        detail="Public market-data reachable",
        checked_at_ms=checked,
        latency_ms=latency,
    )


def timed_ms() -> int:
    return int(time.time() * 1000)
