"""Orchestrate normalized public market-data reads."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.exchanges import build_clients
from app.crypto.models import HealthStatus, SpreadQuote, Ticker
from app.crypto.risk import assert_crypto_read_only, crypto_feature_enabled
from app.crypto.spread import build_spread_quote, validate_instrument_allowlist


def fetch_ticker_for_exchange(
    settings: Settings,
    exchange_id: str,
    symbol: str,
) -> ApiSuccess[Ticker] | ApiFailure:
    assert_crypto_read_only(settings)
    if not crypto_feature_enabled(settings):
        return ApiFailure(
            code="disabled",
            status_code=None,
            retryable=False,
            request_count=0,
            message="Crypto Intelligence disabled",
        )
    instrument_result = validate_instrument_allowlist(symbol, settings.crypto_symbols)
    if isinstance(instrument_result, ApiFailure):
        return instrument_result
    clients = build_clients(settings)
    client = clients.get(exchange_id)
    if client is None:
        return ApiFailure(
            code="unknown_exchange",
            status_code=None,
            retryable=False,
            request_count=0,
            message=f"Unknown exchange: {exchange_id}",
        )
    return client.fetch_ticker(instrument_result.data)


def collect_spread_quotes(
    settings: Settings,
    *,
    symbol: str | None = None,
) -> list[ApiSuccess[SpreadQuote] | ApiFailure]:
    """Collect per-exchange spread quotes for one allowlisted symbol."""
    assert_crypto_read_only(settings)
    symbols = [symbol] if symbol else list(settings.crypto_symbols)
    if not symbols:
        return [
            ApiFailure(
                code="symbol_mismatch",
                status_code=None,
                retryable=False,
                request_count=0,
                message="No crypto symbols configured",
            )
        ]
    target = symbols[0]
    instrument_result = validate_instrument_allowlist(target, settings.crypto_symbols)
    if isinstance(instrument_result, ApiFailure):
        return [instrument_result]

    results: list[ApiSuccess[SpreadQuote] | ApiFailure] = []
    clients = build_clients(settings)
    for exchange_id, client in clients.items():
        ticker_result = client.fetch_ticker(instrument_result.data)
        if isinstance(ticker_result, ApiFailure):
            results.append(ticker_result)
            continue
        spread = build_spread_quote(
            ticker_result.data,
            max_age_seconds=settings.crypto_max_quote_age_seconds,
        )
        results.append(spread)
    return results


def healthy_enough_for_quotes(status: HealthStatus) -> bool:
    return status == HealthStatus.OK
