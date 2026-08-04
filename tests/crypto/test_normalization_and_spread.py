"""Unit tests for instrument normalization, freshness, and spread math."""

from __future__ import annotations

import json
from pathlib import Path

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.exchanges.base import build_ticker
from app.crypto.exchanges.binance import BinancePublicClient
from app.crypto.exchanges.okx import OkxPublicClient
from app.crypto.freshness import require_fresh_timestamp
from app.crypto.models import Instrument, Ticker
from app.crypto.spread import build_spread_quote, compute_spread_pct, validate_instrument_allowlist
from tests.conftest import make_test_settings

FIXTURES = Path(__file__).parent / "fixtures"


def test_instrument_from_canonical_spot():
    inst = Instrument.from_canonical("btc-usdt")
    assert inst is not None
    assert inst.canonical_symbol == "BTC/USDT"
    assert inst.market_type == "spot"


def test_instrument_rejects_empty():
    assert Instrument.from_canonical("") is None
    assert Instrument.from_canonical("BTCUSDT") is None


def test_allowlist_symbol_mismatch():
    result = validate_instrument_allowlist("SOL/USDT", ["BTC/USDT", "ETH/USDT"])
    assert isinstance(result, ApiFailure)
    assert result.code == "symbol_mismatch"


def test_compute_spread_pct():
    ok = compute_spread_pct(100.0, 101.0)
    assert isinstance(ok, ApiSuccess)
    assert abs(ok.data - (1.0 / 100.5) * 100.0) < 1e-9


def test_compute_spread_crossed_book():
    bad = compute_spread_pct(101.0, 100.0)
    assert isinstance(bad, ApiFailure)
    assert bad.code == "crossed_book"


def test_freshness_missing_and_stale():
    missing = require_fresh_timestamp(None, max_age_seconds=30)
    assert isinstance(missing, ApiFailure)
    assert missing.code == "missing_timestamp"
    stale = require_fresh_timestamp(1_000, max_age_seconds=30, now=1_000_000)
    assert isinstance(stale, ApiFailure)
    assert stale.code == "stale_quote"


def test_build_spread_quote_fresh():
    inst = Instrument.from_canonical("BTC/USDT")
    assert inst is not None
    now = 1_710_000_000_000
    ticker = Ticker(
        exchange_id="binance",
        instrument=inst,
        best_bid=100.0,
        best_ask=100.5,
        timestamp_ms=now - 5_000,
    )
    result = build_spread_quote(ticker, max_age_seconds=30, now_ms=now)
    assert isinstance(result, ApiSuccess)
    assert result.data.freshness == "fresh"
    assert result.data.spread_pct > 0


def test_binance_fixture_normalization(monkeypatch):
    settings = make_test_settings(crypto_intel_enabled=True)
    payload = json.loads((FIXTURES / "binance_book_ticker.json").read_text(encoding="utf-8"))

    def fake_get(settings_arg, url, *, params=None, max_attempts=2):
        return ApiSuccess(data=payload, request_count=1)

    monkeypatch.setattr("app.crypto.exchanges.binance.public_get_json", fake_get)
    client = BinancePublicClient(settings)
    inst = Instrument.from_canonical("BTC/USDT")
    assert inst is not None
    result = client.fetch_ticker(inst)
    assert isinstance(result, ApiSuccess)
    assert result.data.best_bid == 65000.10
    assert result.data.best_ask == 65001.20


def test_okx_fixture_requires_timestamp(monkeypatch):
    settings = make_test_settings(crypto_intel_enabled=True)
    payload = json.loads((FIXTURES / "okx_ticker.json").read_text(encoding="utf-8"))

    def fake_get(settings_arg, url, *, params=None, max_attempts=2):
        return ApiSuccess(data=payload, request_count=1)

    monkeypatch.setattr("app.crypto.exchanges.okx.public_get_json", fake_get)
    client = OkxPublicClient(settings)
    inst = Instrument.from_canonical("BTC/USDT")
    assert inst is not None
    result = client.fetch_ticker(inst)
    assert isinstance(result, ApiSuccess)
    assert result.data.timestamp_ms == 1710000000000


def test_build_ticker_missing_timestamp_fail_closed():
    inst = Instrument.from_canonical("BTC/USDT")
    assert inst is not None
    result = build_ticker(
        exchange_id="okx",
        instrument=inst,
        best_bid=1,
        best_ask=2,
        timestamp_ms=None,
        allow_receive_time=False,
    )
    assert isinstance(result, ApiFailure)
    assert result.code == "missing_timestamp"
