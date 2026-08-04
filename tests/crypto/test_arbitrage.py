"""Synthetic tests for V3.0.0c gross arbitrage scanner."""

from __future__ import annotations

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.arbitrage import collect_arbitrage_opportunities
from app.crypto.models import ExchangeHealth, HealthStatus, Instrument, Ticker
from app.telegram.crypto.formatting import ARB_DISCLAIMER, format_arbitrage_opportunities
from tests.conftest import make_test_settings


class _FakeClient:
    def __init__(self, exchange_id: str, ticker: Ticker | ApiFailure) -> None:
        self.exchange_id = exchange_id
        self.display_name = exchange_id
        self._ticker = ticker

    def fetch_ticker(self, instrument: Instrument):
        return self._ticker

    def probe_health(self) -> ExchangeHealth:
        return ExchangeHealth(
            exchange_id=self.exchange_id,
            status=HealthStatus.OK,
            detail="ok",
            checked_at_ms=1,
            latency_ms=1.0,
        )


def _ticker(
    exchange_id: str,
    *,
    bid: float,
    ask: float,
    now_ms: int,
    symbol: str = "BTC/USDT",
) -> Ticker:
    inst = Instrument.from_canonical(symbol)
    assert inst is not None
    return Ticker(
        exchange_id=exchange_id,
        instrument=inst,
        best_bid=bid,
        best_ask=ask,
        timestamp_ms=now_ms - 500,
    )


def _patch_fresh(monkeypatch):
    monkeypatch.setattr(
        "app.crypto.arbitrage.require_fresh_timestamp",
        lambda ts, *, max_age_seconds, now=None, request_count=1: ApiSuccess(
            data=(ts, 0.5)
        ),
    )
    monkeypatch.setattr("app.crypto.arbitrage.now_ms", lambda: 1_710_000_000_000)


def test_disabled_arb():
    settings = make_test_settings(crypto_intel_enabled=False)
    report = collect_arbitrage_opportunities(settings)
    assert report.enabled is False
    assert report.opportunities == ()
    text = format_arbitrage_opportunities(report)
    assert "disabled" in text.lower()
    assert "Not guaranteed profit" in ARB_DISCLAIMER


def test_valid_best_candidate_across_exchanges(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(
        crypto_intel_enabled=True,
        crypto_symbols=["BTC/USDT"],
    )
    # Buy cheapest ask on binance (100), sell highest bid on okx (102) → gross 2
    clients = {
        "binance": _FakeClient(
            "binance",
            ApiSuccess(data=_ticker("binance", bid=99.5, ask=100.0, now_ms=now)),
        ),
        "okx": _FakeClient(
            "okx",
            ApiSuccess(data=_ticker("okx", bid=102.0, ask=102.5, now_ms=now)),
        ),
        "bybit": _FakeClient(
            "bybit",
            ApiSuccess(data=_ticker("bybit", bid=101.0, ask=101.2, now_ms=now)),
        ),
    }
    health = [
        ExchangeHealth(eid, HealthStatus.OK, "ok", now, 1.0)
        for eid in clients
    ]
    monkeypatch.setattr("app.crypto.arbitrage.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.arbitrage.build_clients", lambda s: clients)
    _patch_fresh(monkeypatch)
    report = collect_arbitrage_opportunities(settings)
    assert len(report.opportunities) == 1
    opp = report.opportunities[0]
    assert opp.buy_exchange_id == "binance"
    assert opp.sell_exchange_id == "okx"
    assert opp.buy_ask == 100.0
    assert opp.sell_bid == 102.0
    assert opp.gross_spread_abs == 2.0
    assert opp.market_type == "spot"
    assert opp.gross_spread_pct > 0
    text = format_arbitrage_opportunities(report)
    assert "Gross price difference" in text
    assert "not an executable" in text.lower()


def test_insufficient_exchanges(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(crypto_intel_enabled=True, crypto_symbols=["BTC/USDT"])
    clients = {
        "binance": _FakeClient(
            "binance",
            ApiSuccess(data=_ticker("binance", bid=99.0, ask=100.0, now_ms=now)),
        ),
    }
    health = [ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0)]
    monkeypatch.setattr("app.crypto.arbitrage.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.arbitrage.build_clients", lambda s: clients)
    _patch_fresh(monkeypatch)
    report = collect_arbitrage_opportunities(settings)
    assert report.opportunities == ()
    assert any(r.code == "insufficient_exchanges" for r in report.rejects)


def test_no_positive_gross_spread(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(crypto_intel_enabled=True, crypto_symbols=["BTC/USDT"])
    # Both venues same-ish; sell bid never above other ask
    clients = {
        "binance": _FakeClient(
            "binance",
            ApiSuccess(data=_ticker("binance", bid=100.0, ask=100.5, now_ms=now)),
        ),
        "okx": _FakeClient(
            "okx",
            ApiSuccess(data=_ticker("okx", bid=100.1, ask=100.6, now_ms=now)),
        ),
    }
    health = [
        ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0),
        ExchangeHealth("okx", HealthStatus.OK, "ok", now, 1.0),
    ]
    monkeypatch.setattr("app.crypto.arbitrage.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.arbitrage.build_clients", lambda s: clients)
    _patch_fresh(monkeypatch)
    report = collect_arbitrage_opportunities(settings)
    assert report.opportunities == ()
    assert any(r.code == "no_positive_gross_spread" for r in report.rejects)


def test_degraded_health_and_partial_failure(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(crypto_intel_enabled=True, crypto_symbols=["BTC/USDT"])
    clients = {
        "binance": _FakeClient(
            "binance",
            ApiSuccess(data=_ticker("binance", bid=99.0, ask=100.0, now_ms=now)),
        ),
        "okx": _FakeClient(
            "okx",
            ApiFailure(
                code="invalid_json",
                status_code=200,
                retryable=False,
                request_count=1,
                message="bad",
            ),
        ),
        "bybit": _FakeClient(
            "bybit",
            ApiSuccess(data=_ticker("bybit", bid=103.0, ask=103.5, now_ms=now)),
        ),
    }
    health = [
        ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0),
        ExchangeHealth("okx", HealthStatus.OK, "ok", now, 1.0),
        ExchangeHealth("bybit", HealthStatus.DEGRADED, "slow", now, 40.0),
    ]
    monkeypatch.setattr("app.crypto.arbitrage.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.arbitrage.build_clients", lambda s: clients)
    _patch_fresh(monkeypatch)
    report = collect_arbitrage_opportunities(settings)
    # bybit degraded, okx malformed → only binance valid → insufficient
    assert report.opportunities == ()
    assert any(r.code == "exchange_degraded" for r in report.rejects)
    assert any(r.code == "invalid_json" for r in report.rejects)
    assert any(r.code == "insufficient_exchanges" for r in report.rejects)


def test_crossed_and_stale(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(crypto_intel_enabled=True, crypto_symbols=["BTC/USDT"])
    crossed = _ticker("binance", bid=101.0, ask=100.0, now_ms=now)
    clients = {
        "binance": _FakeClient("binance", ApiSuccess(data=crossed)),
        "okx": _FakeClient(
            "okx",
            ApiSuccess(data=_ticker("okx", bid=102.0, ask=102.5, now_ms=now)),
        ),
    }
    health = [
        ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0),
        ExchangeHealth("okx", HealthStatus.OK, "ok", now, 1.0),
    ]
    monkeypatch.setattr("app.crypto.arbitrage.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.arbitrage.build_clients", lambda s: clients)
    monkeypatch.setattr("app.crypto.arbitrage.now_ms", lambda: now)

    def fresh(ts, *, max_age_seconds, now=None, request_count=1):
        # Force stale for all
        return ApiFailure(
            code="stale_quote",
            status_code=None,
            retryable=True,
            request_count=1,
            message="stale",
        )

    monkeypatch.setattr("app.crypto.arbitrage.require_fresh_timestamp", fresh)
    report = collect_arbitrage_opportunities(settings)
    assert report.opportunities == ()
    assert any(r.code == "stale_quote" for r in report.rejects)


def test_unsupported_symbol(monkeypatch):
    settings = make_test_settings(
        crypto_intel_enabled=True,
        crypto_symbols=["NOTASUIT"],
    )
    monkeypatch.setattr("app.crypto.arbitrage.probe_all_exchanges", lambda s: [])
    monkeypatch.setattr("app.crypto.arbitrage.build_clients", lambda s: {})
    report = collect_arbitrage_opportunities(settings)
    assert any(
        r.code in {"unsupported_market_type", "symbol_mismatch"} for r in report.rejects
    )


def test_same_symbol_enforced_in_selection(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(
        crypto_intel_enabled=True,
        crypto_symbols=["BTC/USDT", "ETH/USDT"],
    )
    clients = {
        "binance": _FakeClient(
            "binance",
            ApiSuccess(
                data=_ticker("binance", bid=99.0, ask=100.0, now_ms=now, symbol="BTC/USDT")
            ),
        ),
        "okx": _FakeClient(
            "okx",
            ApiSuccess(
                data=_ticker("okx", bid=105.0, ask=105.5, now_ms=now, symbol="BTC/USDT")
            ),
        ),
    }
    # For ETH, return failures so only BTC yields opportunity
    eth_fail = ApiFailure(
        code="http_error",
        status_code=None,
        retryable=True,
        request_count=1,
        message="x",
    )

    class _Multi(_FakeClient):
        def fetch_ticker(self, instrument: Instrument):
            if instrument.canonical_symbol == "ETH/USDT":
                return eth_fail
            return self._ticker

    clients = {
        "binance": _Multi(
            "binance",
            ApiSuccess(
                data=_ticker("binance", bid=99.0, ask=100.0, now_ms=now, symbol="BTC/USDT")
            ),
        ),
        "okx": _Multi(
            "okx",
            ApiSuccess(
                data=_ticker("okx", bid=105.0, ask=105.5, now_ms=now, symbol="BTC/USDT")
            ),
        ),
    }
    health = [
        ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0),
        ExchangeHealth("okx", HealthStatus.OK, "ok", now, 1.0),
    ]
    monkeypatch.setattr("app.crypto.arbitrage.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.arbitrage.build_clients", lambda s: clients)
    _patch_fresh(monkeypatch)
    report = collect_arbitrage_opportunities(settings)
    assert len(report.opportunities) == 1
    assert report.opportunities[0].instrument.canonical_symbol == "BTC/USDT"
