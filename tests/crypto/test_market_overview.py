"""Synthetic tests for V3.0.0b Market Overview."""

from __future__ import annotations

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.models import (
    ExchangeHealth,
    HealthStatus,
    Instrument,
    OverviewReport,
    Ticker,
)
from app.crypto.overview import collect_market_overview
from app.telegram.crypto.formatting import format_market_overview
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


def _fresh_ticker(exchange_id: str, symbol: str = "BTC/USDT", *, now_ms: int) -> Ticker:
    inst = Instrument.from_canonical(symbol)
    assert inst is not None
    return Ticker(
        exchange_id=exchange_id,
        instrument=inst,
        best_bid=100.0,
        best_ask=100.5,
        timestamp_ms=now_ms - 1_000,
    )


def test_disabled_overview_no_rows():
    settings = make_test_settings(crypto_intel_enabled=False)
    report = collect_market_overview(settings)
    assert report.enabled is False
    assert report.rows == ()
    text = format_market_overview(report)
    assert "disabled" in text.lower()


def test_successful_multi_exchange_overview(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(
        crypto_intel_enabled=True,
        crypto_symbols=["BTC/USDT"],
        crypto_max_quote_age_seconds=30.0,
    )
    clients = {
        "binance": _FakeClient(
            "binance", ApiSuccess(data=_fresh_ticker("binance", now_ms=now))
        ),
        "okx": _FakeClient("okx", ApiSuccess(data=_fresh_ticker("okx", now_ms=now))),
    }
    health = [
        ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0),
        ExchangeHealth("okx", HealthStatus.OK, "ok", now, 1.0),
    ]
    monkeypatch.setattr("app.crypto.overview.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.overview.build_clients", lambda s: clients)
    monkeypatch.setattr(
        "app.crypto.overview.require_fresh_timestamp",
        lambda ts, *, max_age_seconds, now=None, request_count=1: ApiSuccess(
            data=(ts, 1.0)
        ),
    )
    report = collect_market_overview(settings)
    assert len(report.rows) == 2
    assert all(r.midpoint == 100.25 for r in report.rows)
    assert all(r.market_type == "spot" for r in report.rows)
    text = format_market_overview(report)
    assert "mid=" in text
    assert "guaranteed profit" not in text.lower() or "not guaranteed" in text.lower()


def test_degraded_health_rejects_venue(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(crypto_intel_enabled=True, crypto_symbols=["BTC/USDT"])
    clients = {
        "binance": _FakeClient(
            "binance", ApiSuccess(data=_fresh_ticker("binance", now_ms=now))
        ),
    }
    health = [ExchangeHealth("binance", HealthStatus.DEGRADED, "slow", now, 40.0)]
    monkeypatch.setattr("app.crypto.overview.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.overview.build_clients", lambda s: clients)
    report = collect_market_overview(settings)
    assert report.rows == ()
    assert any(r.code == "exchange_degraded" for r in report.rejects)


def test_unsupported_symbol_reject(monkeypatch):
    settings = make_test_settings(
        crypto_intel_enabled=True,
        crypto_symbols=["NOTASUIT"],
    )
    monkeypatch.setattr("app.crypto.overview.probe_all_exchanges", lambda s: [])
    monkeypatch.setattr("app.crypto.overview.build_clients", lambda s: {})
    report = collect_market_overview(settings)
    assert report.rows == ()
    assert any(
        r.code in {"unsupported_market_type", "symbol_mismatch"} for r in report.rejects
    )


def test_crossed_book_reject(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(crypto_intel_enabled=True, crypto_symbols=["BTC/USDT"])
    inst = Instrument.from_canonical("BTC/USDT")
    assert inst is not None
    bad = Ticker(
        exchange_id="binance",
        instrument=inst,
        best_bid=101.0,
        best_ask=100.0,
        timestamp_ms=now,
    )
    clients = {"binance": _FakeClient("binance", ApiSuccess(data=bad))}
    health = [ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0)]
    monkeypatch.setattr("app.crypto.overview.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.overview.build_clients", lambda s: clients)
    report = collect_market_overview(settings)
    assert report.rows == ()
    assert any(r.code == "crossed_book" for r in report.rejects)


def test_stale_quote_reject(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(
        crypto_intel_enabled=True,
        crypto_symbols=["BTC/USDT"],
        crypto_max_quote_age_seconds=30.0,
    )
    clients = {
        "binance": _FakeClient(
            "binance", ApiSuccess(data=_fresh_ticker("binance", now_ms=now))
        ),
    }
    health = [ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0)]
    monkeypatch.setattr("app.crypto.overview.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.overview.build_clients", lambda s: clients)
    monkeypatch.setattr(
        "app.crypto.overview.require_fresh_timestamp",
        lambda *a, **k: ApiFailure(
            code="stale_quote",
            status_code=None,
            retryable=True,
            request_count=1,
            message="stale",
        ),
    )
    report = collect_market_overview(settings)
    assert report.rows == ()
    assert any(r.code == "stale_quote" for r in report.rejects)


def test_malformed_ticker_reject(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(crypto_intel_enabled=True, crypto_symbols=["BTC/USDT"])
    clients = {
        "binance": _FakeClient(
            "binance",
            ApiFailure(
                code="invalid_json",
                status_code=200,
                retryable=False,
                request_count=1,
                message="bad",
            ),
        ),
    }
    health = [ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0)]
    monkeypatch.setattr("app.crypto.overview.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.overview.build_clients", lambda s: clients)
    report = collect_market_overview(settings)
    assert any(r.code == "invalid_json" for r in report.rejects)


def test_partial_failure_mixed(monkeypatch):
    now = 1_710_000_000_000
    settings = make_test_settings(crypto_intel_enabled=True, crypto_symbols=["BTC/USDT"])
    clients = {
        "binance": _FakeClient(
            "binance", ApiSuccess(data=_fresh_ticker("binance", now_ms=now))
        ),
        "bybit": _FakeClient(
            "bybit",
            ApiFailure(
                code="http_error",
                status_code=None,
                retryable=True,
                request_count=1,
                message="down",
            ),
        ),
    }
    health = [
        ExchangeHealth("binance", HealthStatus.OK, "ok", now, 1.0),
        ExchangeHealth("bybit", HealthStatus.OK, "ok", now, 1.0),
    ]
    monkeypatch.setattr("app.crypto.overview.probe_all_exchanges", lambda s: health)
    monkeypatch.setattr("app.crypto.overview.build_clients", lambda s: clients)
    monkeypatch.setattr(
        "app.crypto.overview.require_fresh_timestamp",
        lambda ts, *, max_age_seconds, now=None, request_count=1: ApiSuccess(
            data=(ts, 0.5)
        ),
    )
    report = collect_market_overview(settings)
    assert len(report.rows) == 1
    assert len(report.rejects) == 1
    text = format_market_overview(report)
    assert "Rejected" in text


def test_format_truncates_rejects():
    rejects = [
        __import__("app.crypto.models", fromlist=["OverviewReject"]).OverviewReject(
            exchange_id=f"e{i}", symbol="BTC/USDT", code="x", message="m"
        )
        for i in range(20)
    ]
    report = OverviewReport(
        enabled=True,
        symbols=("BTC/USDT",),
        rows=(),
        rejects=tuple(rejects),
    )
    text = format_market_overview(report, max_reject_lines=5)
    assert "+15 more rejected" in text
