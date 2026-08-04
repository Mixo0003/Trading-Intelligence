"""OKX public spot market-data adapter."""

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
from app.crypto.models import ExchangeHealth, Instrument, Ticker


class OkxPublicClient:
    exchange_id = "okx"
    display_name = "OKX"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base = settings.crypto_okx_base_url

    def fetch_ticker(self, instrument: Instrument) -> ApiSuccess[Ticker] | ApiFailure:
        inst_id = venue_symbol_dash(instrument)
        result = public_get_json(
            self._settings,
            f"{self._base}/api/v5/market/ticker",
            params={"instId": inst_id},
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
                message="Unexpected OKX payload",
            )
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return ApiFailure(
                code="empty_book",
                status_code=None,
                retryable=False,
                request_count=result.request_count,
                message="OKX ticker empty",
            )
        row = data[0]
        return build_ticker(
            exchange_id=self.exchange_id,
            instrument=instrument,
            best_bid=row.get("bidPx"),
            best_ask=row.get("askPx"),
            timestamp_ms=row.get("ts"),
            request_count=result.request_count,
            allow_receive_time=False,
        )

    def probe_health(self) -> ExchangeHealth:
        started = timed_ms()
        result = public_get_json(self._settings, f"{self._base}/api/v5/public/time")
        return health_from_probe(self.exchange_id, result, started_ms=started)
