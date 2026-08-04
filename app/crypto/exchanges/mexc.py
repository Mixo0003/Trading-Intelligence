"""MEXC public spot market-data adapter."""

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


class MexcPublicClient:
    exchange_id = "mexc"
    display_name = "MEXC"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base = settings.crypto_mexc_base_url

    def fetch_ticker(self, instrument: Instrument) -> ApiSuccess[Ticker] | ApiFailure:
        symbol = venue_symbol_compact(instrument)
        result = public_get_json(
            self._settings,
            f"{self._base}/api/v3/ticker/bookTicker",
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
                message="Unexpected MEXC payload",
            )
        return build_ticker(
            exchange_id=self.exchange_id,
            instrument=instrument,
            best_bid=payload.get("bidPrice"),
            best_ask=payload.get("askPrice"),
            timestamp_ms=None,
            request_count=result.request_count,
            allow_receive_time=True,
        )

    def probe_health(self) -> ExchangeHealth:
        started = timed_ms()
        result = public_get_json(self._settings, f"{self._base}/api/v3/ping")
        return health_from_probe(self.exchange_id, result, started_ms=started)
