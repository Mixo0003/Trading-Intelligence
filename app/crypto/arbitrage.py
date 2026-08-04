"""Cross-exchange gross arbitrage scanner — read-only, paper-only (V3.0.0c)."""

from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings
from app.crypto.api_result import ApiFailure
from app.crypto.exchanges import build_clients
from app.crypto.freshness import now_ms, require_fresh_timestamp
from app.crypto.health import probe_all_exchanges
from app.crypto.market_data import healthy_enough_for_quotes
from app.crypto.models import (
    ArbReject,
    ArbReport,
    GrossArbOpportunity,
    HealthStatus,
    Instrument,
    Ticker,
)
from app.crypto.risk import assert_crypto_read_only, crypto_feature_enabled
from app.crypto.spread import validate_instrument_allowlist


@dataclass(frozen=True)
class _ValidQuote:
    exchange_id: str
    instrument: Instrument
    best_bid: float
    best_ask: float
    timestamp_ms: int
    age_seconds: float


def _reject(
    *,
    exchange_id: str | None,
    symbol: str | None,
    code: str,
    message: str,
) -> ArbReject:
    return ArbReject(
        exchange_id=exchange_id,
        symbol=symbol,
        code=code,
        message=message,
    )


def _validate_ticker(
    ticker: Ticker,
    *,
    max_age_seconds: float,
) -> ApiFailure | tuple[float, float, int, float]:
    if ticker.best_bid <= 0 or ticker.best_ask <= 0:
        return ApiFailure(
            code="invalid_quote",
            status_code=None,
            retryable=False,
            request_count=0,
            message="Missing or invalid bid/ask",
        )
    if ticker.best_ask < ticker.best_bid:
        return ApiFailure(
            code="crossed_book",
            status_code=None,
            retryable=False,
            request_count=0,
            message="Ask is below bid",
        )
    fresh = require_fresh_timestamp(
        ticker.timestamp_ms,
        max_age_seconds=max_age_seconds,
    )
    if isinstance(fresh, ApiFailure):
        return fresh
    _ts, age = fresh.data
    return ticker.best_bid, ticker.best_ask, ticker.timestamp_ms, age


def _best_gross_for_quotes(
    quotes: list[_ValidQuote],
    *,
    receive_ms: int,
) -> GrossArbOpportunity | None:
    best: GrossArbOpportunity | None = None
    for buy in quotes:
        for sell in quotes:
            if buy.exchange_id == sell.exchange_id:
                continue
            if buy.instrument.canonical_symbol != sell.instrument.canonical_symbol:
                continue
            if buy.instrument.market_type != "spot" or sell.instrument.market_type != "spot":
                continue
            gross_abs = sell.best_bid - buy.best_ask
            if gross_abs <= 0:
                continue
            mid = (buy.best_ask + sell.best_bid) / 2.0
            if mid <= 0:
                continue
            gross_pct = (gross_abs / mid) * 100.0
            age = max(buy.age_seconds, sell.age_seconds)
            candidate = GrossArbOpportunity(
                instrument=buy.instrument,
                market_type="spot",
                buy_exchange_id=buy.exchange_id,
                buy_ask=buy.best_ask,
                sell_exchange_id=sell.exchange_id,
                sell_bid=sell.best_bid,
                gross_spread_abs=gross_abs,
                gross_spread_pct=gross_pct,
                buy_timestamp_ms=buy.timestamp_ms,
                sell_timestamp_ms=sell.timestamp_ms,
                receive_timestamp_ms=receive_ms,
                age_seconds=age,
                health_status=HealthStatus.OK,
            )
            if best is None or candidate.gross_spread_pct > best.gross_spread_pct:
                best = candidate
            elif (
                best is not None
                and candidate.gross_spread_pct == best.gross_spread_pct
                and candidate.gross_spread_abs > best.gross_spread_abs
            ):
                best = candidate
    return best


def collect_arbitrage_opportunities(settings: Settings) -> ArbReport:
    """Best positive gross cross-venue gap per CRYPTO_SYMBOLS (spot only)."""
    assert_crypto_read_only(settings)
    symbols = tuple(settings.crypto_symbols)
    if not crypto_feature_enabled(settings):
        return ArbReport(enabled=False, symbols=symbols, opportunities=(), rejects=())

    rejects: list[ArbReject] = []
    opportunities: list[GrossArbOpportunity] = []

    if not symbols:
        rejects.append(
            _reject(
                exchange_id=None,
                symbol=None,
                code="symbol_mismatch",
                message="No crypto symbols configured",
            )
        )
        return ArbReport(
            enabled=True,
            symbols=symbols,
            opportunities=(),
            rejects=tuple(rejects),
        )

    health_rows = probe_all_exchanges(settings)
    health_by_id = {h.exchange_id: h for h in health_rows}
    clients = build_clients(settings)
    receive = now_ms()

    for symbol in symbols:
        instrument_result = validate_instrument_allowlist(
            symbol, list(settings.crypto_symbols)
        )
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

        valid: list[_ValidQuote] = []
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

            checked = _validate_ticker(
                ticker_result.data,
                max_age_seconds=settings.crypto_max_quote_age_seconds,
            )
            if isinstance(checked, ApiFailure):
                rejects.append(
                    _reject(
                        exchange_id=exchange_id,
                        symbol=instrument.canonical_symbol,
                        code=checked.code,
                        message=checked.message,
                    )
                )
                continue
            bid, ask, ts, age = checked
            valid.append(
                _ValidQuote(
                    exchange_id=exchange_id,
                    instrument=instrument,
                    best_bid=bid,
                    best_ask=ask,
                    timestamp_ms=ts,
                    age_seconds=age,
                )
            )

        if len(valid) < 2:
            rejects.append(
                _reject(
                    exchange_id=None,
                    symbol=instrument.canonical_symbol,
                    code="insufficient_exchanges",
                    message="Need at least two healthy exchanges with fresh quotes",
                )
            )
            continue

        best = _best_gross_for_quotes(valid, receive_ms=receive)
        if best is None:
            rejects.append(
                _reject(
                    exchange_id=None,
                    symbol=instrument.canonical_symbol,
                    code="no_positive_gross_spread",
                    message="No valid positive gross cross-exchange price gap",
                )
            )
            continue
        opportunities.append(best)

    return ArbReport(
        enabled=True,
        symbols=symbols,
        opportunities=tuple(opportunities),
        rejects=tuple(rejects),
    )
