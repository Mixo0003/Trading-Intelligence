"""BingX public spot market-data adapter."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.exchanges.base import (
    build_ticker,
    health_from_probe,
    public_get_json,
    timed_ms,
    venue_symbol_dash,
)
from app.crypto.models import ExchangeHealth, Instrument, OrderBookSnapshot, Ticker
from app.crypto.order_book import DEFAULT_BOOK_LIMIT, build_order_book_snapshot


class BingxPublicClient:
    exchange_id = "bingx"
    display_name = "BingX"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base = settings.crypto_bingx_base_url

    def fetch_ticker(self, instrument: Instrument) -> ApiSuccess[Ticker] | ApiFailure:
        symbol = venue_symbol_dash(instrument)
        result = public_get_json(
            self._settings,
            f"{self._base}/openApi/spot/v1/ticker/bookTicker",
            params={"symbol": symbol},
        )
        if isinstance(result, ApiFailure):
            return result
        payload = result.data
        if not isinstance(payload, dict):
            return ApiFailure(
                code="invalid_payload",
                status_code=None,
                retryable=False,
                request_count=result.request_count,
                message="Unexpected BingX payload",
            )
        data = payload.get("data")
        row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict):
            return ApiFailure(
                code="empty_book",
                status_code=None,
                retryable=False,
                request_count=result.request_count,
                message="BingX ticker empty",
            )
        return build_ticker(
            exchange_id=self.exchange_id,
            instrument=instrument,
            best_bid=row.get("bidPrice") or row.get("bid"),
            best_ask=row.get("askPrice") or row.get("ask"),
            timestamp_ms=row.get("time") or payload.get("timestamp"),
            request_count=result.request_count,
            allow_receive_time=True,
        )

    def fetch_order_book(
        self,
        instrument: Instrument,
        *,
        limit: int = DEFAULT_BOOK_LIMIT,
    ) -> ApiSuccess[OrderBookSnapshot] | ApiFailure:
        symbol = venue_symbol_dash(instrument)
        result = public_get_json(
            self._settings,
            f"{self._base}/openApi/spot/v1/market/depth",
            params={"symbol": symbol, "limit": int(limit)},
        )
        if isinstance(result, ApiFailure):
            return result
        payload = result.data
        if not isinstance(payload, dict):
            return ApiFailure(
                code="invalid_payload",
                status_code=None,
                retryable=False,
                request_count=result.request_count,
                message="Unexpected BingX depth payload",
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            return ApiFailure(
                code="empty_book",
                status_code=None,
                retryable=False,
                request_count=result.request_count,
                message="BingX depth empty",
            )
        return build_order_book_snapshot(
            exchange_id=self.exchange_id,
            instrument=instrument,
            bids_raw=data.get("bids"),
            asks_raw=data.get("asks"),
            timestamp_ms=data.get("ts") or payload.get("timestamp"),
            request_count=result.request_count,
            allow_receive_time=True,
            max_age_seconds=self._settings.crypto_max_quote_age_seconds,
        )

    def probe_health(self) -> ExchangeHealth:
        started = timed_ms()
        # Lightweight public ticker probe for a common spot pair.
        result = public_get_json(
            self._settings,
            f"{self._base}/openApi/spot/v1/ticker/24hr",
            params={"symbol": "BTC-USDT"},
        )
        return health_from_probe(self.exchange_id, result, started_ms=started)
