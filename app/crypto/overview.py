"""Market Overview orchestrator — multi-symbol spot watchlist (V3.0.0b)."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.api_result import ApiFailure
from app.crypto.exchanges import build_clients
from app.crypto.freshness import require_fresh_timestamp
from app.crypto.health import probe_all_exchanges
from app.crypto.market_data import healthy_enough_for_quotes
from app.crypto.models import (
    HealthStatus,
    OverviewReject,
    OverviewReport,
    OverviewRow,
)
from app.crypto.risk import assert_crypto_read_only, crypto_feature_enabled
from app.crypto.spread import validate_instrument_allowlist


def _reject(
    *,
    exchange_id: str | None,
    symbol: str | None,
    code: str,
    message: str,
) -> OverviewReject:
    return OverviewReject(
        exchange_id=exchange_id,
        symbol=symbol,
        code=code,
        message=message,
    )


def collect_market_overview(settings: Settings) -> OverviewReport:
    """Collect fail-closed overview rows for CRYPTO_SYMBOLS × healthy exchanges."""
    assert_crypto_read_only(settings)
    symbols = tuple(settings.crypto_symbols)
    if not crypto_feature_enabled(settings):
        return OverviewReport(enabled=False, symbols=symbols, rows=(), rejects=())

    rejects: list[OverviewReject] = []
    rows: list[OverviewRow] = []

    if not symbols:
        rejects.append(
            _reject(
                exchange_id=None,
                symbol=None,
                code="symbol_mismatch",
                message="No crypto symbols configured",
            )
        )
        return OverviewReport(
            enabled=True, symbols=symbols, rows=(), rejects=tuple(rejects)
        )

    health_rows = probe_all_exchanges(settings)
    health_by_id = {h.exchange_id: h for h in health_rows}
    clients = build_clients(settings)

    for symbol in symbols:
        instrument_result = validate_instrument_allowlist(symbol, list(settings.crypto_symbols))
        if isinstance(instrument_result, ApiFailure):
            rejects.append(
                _reject(
                    exchange_id=None,
                    symbol=symbol,
                    code=instrument_result.code,
                    message=instrument_result.message,
                )
            )
            continue
        instrument = instrument_result.data
        if instrument.market_type != "spot":
            rejects.append(
                _reject(
                    exchange_id=None,
                    symbol=symbol,
                    code="unsupported_market_type",
                    message=f"Unsupported market type: {instrument.market_type}",
                )
            )
            continue

        for exchange_id, client in clients.items():
            health = health_by_id.get(exchange_id)
            if health is None or not healthy_enough_for_quotes(health.status):
                status = health.status.value if health else HealthStatus.DOWN.value
                rejects.append(
                    _reject(
                        exchange_id=exchange_id,
                        symbol=instrument.canonical_symbol,
                        code="exchange_degraded",
                        message=f"Exchange health not ok ({status})",
                    )
                )
                continue

            ticker_result = client.fetch_ticker(instrument)
            if isinstance(ticker_result, ApiFailure):
                rejects.append(
                    _reject(
                        exchange_id=exchange_id,
                        symbol=instrument.canonical_symbol,
                        code=ticker_result.code,
                        message=ticker_result.message,
                    )
                )
                continue

            ticker = ticker_result.data
            if ticker.best_bid <= 0 or ticker.best_ask <= 0:
                rejects.append(
                    _reject(
                        exchange_id=exchange_id,
                        symbol=instrument.canonical_symbol,
                        code="invalid_quote",
                        message="Missing or invalid bid/ask",
                    )
                )
                continue
            if ticker.best_ask < ticker.best_bid:
                rejects.append(
                    _reject(
                        exchange_id=exchange_id,
                        symbol=instrument.canonical_symbol,
                        code="crossed_book",
                        message="Ask is below bid",
                    )
                )
                continue

            fresh = require_fresh_timestamp(
                ticker.timestamp_ms,
                max_age_seconds=settings.crypto_max_quote_age_seconds,
            )
            if isinstance(fresh, ApiFailure):
                rejects.append(
                    _reject(
                        exchange_id=exchange_id,
                        symbol=instrument.canonical_symbol,
                        code=fresh.code,
                        message=fresh.message,
                    )
                )
                continue
            _ts, age = fresh.data
            mid = (ticker.best_bid + ticker.best_ask) / 2.0
            rows.append(
                OverviewRow(
                    exchange_id=exchange_id,
                    instrument=instrument,
                    market_type="spot",
                    best_bid=ticker.best_bid,
                    best_ask=ticker.best_ask,
                    midpoint=mid,
                    timestamp_ms=ticker.timestamp_ms,
                    age_seconds=age,
                    freshness="fresh",
                    health_status=HealthStatus.OK,
                )
            )

    return OverviewReport(
        enabled=True,
        symbols=symbols,
        rows=tuple(rows),
        rejects=tuple(rejects),
    )
