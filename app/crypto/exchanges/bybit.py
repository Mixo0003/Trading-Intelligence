"""Bybit public spot market-data adapter."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.exchanges.base import (
    build_ticker,
    health_from_probe,
    public_get_json,
    timed_ms,
    venue_symbol_compact,
)
from app.crypto.models import ExchangeHealth, Instrument, Ticker


class BybitPublicClient:
    exchange_id = "bybit"
    display_name = "Bybit"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base = settings.crypto_bybit_base_url

    def fetch_ticker(self, instrument: Instrument) -> ApiSuccess[Ticker] | ApiFailure:
        symbol = venue_symbol_compact(instrument)
        result = public_get_json(
            self._settings,
            f"{self._base}/v5/market/tickers",
            params={"category": "spot", "symbol": symbol},
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
                message="Unexpected Bybit payload",
            )
        lst = (payload.get("result") or {}).get("list") if isinstance(payload.get("result"), dict) else None
        if not isinstance(lst, list) or not lst:
            return ApiFailure(
                code="empty_book",
                status_code=None,
                retryable=False,
                request_count=result.request_count,
                message="Bybit ticker empty",
            )
        row = lst[0]
        if not isinstance(row, dict):
            return ApiFailure(
                code="invalid_payload",
                status_code=None,
                retryable=False,
                request_count=result.request_count,
                message="Bybit ticker row invalid",
            )
        return build_ticker(
            exchange_id=self.exchange_id,
            instrument=instrument,
            best_bid=row.get("bid1Price"),
            best_ask=row.get("ask1Price"),
            timestamp_ms=payload.get("time") or row.get("ts"),
            request_count=result.request_count,
            allow_receive_time=True,
        )

    def probe_health(self) -> ExchangeHealth:
        started = timed_ms()
        result = public_get_json(
            self._settings,
            f"{self._base}/v5/market/time",
        )
        return health_from_probe(self.exchange_id, result, started_ms=started)
