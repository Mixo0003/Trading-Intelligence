"""Focused offline tests for V4.4 catalog-driven spot paper arbitrage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.catalog_discovery import discover_spot_catalog
from app.crypto.discovery_models import DiscoveryCompleteness
from app.crypto.exchanges import build_clients, build_v43_clients
from app.crypto.freshness import now_ms
from app.crypto.models import BookLevel, EXCHANGE_IDS, Instrument, OrderBookSnapshot
from app.crypto.opportunity.formatting import format_crypto_opportunity_card
from app.crypto.opportunity.models import PaperNetOpportunity
from app.crypto.opportunity.netting import evaluate_cross_venue_opportunity
from app.crypto.opportunity.scanner import scan_crypto_opportunities
from app.crypto.opportunity.settings import balanced_crypto_settings
from app.crypto.opportunity.universe import build_symbol_universe
from app.crypto.order_book import allocate_buy_usd, allocate_sell_base
from app.crypto.venue_registry import V43_EXCHANGE_IDS
from app.database.market_opportunity_storage import (
    initialize_market_opportunity_schema,
    list_catalog_instruments,
    persist_catalog_discovery_result,
)
from app.database.storage import initialize_database
from tests.conftest import make_test_settings


def _inst(symbol: str = "BTC/USDT") -> Instrument:
    out = Instrument.from_canonical(symbol)
    assert out is not None
    return out


def _book(
    exchange_id: str,
    *,
    bid: float = 100.0,
    ask: float = 101.0,
    bid_size: float = 10.0,
    ask_size: float = 10.0,
    ts_ms: int | None = None,
    symbol: str = "BTC/USDT",
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange_id=exchange_id,
        instrument=_inst(symbol),
        bids=(BookLevel(price=bid, size=bid_size),),
        asks=(BookLevel(price=ask, size=ask_size),),
        timestamp_ms=ts_ms if ts_ms is not None else now_ms(),
    )


def _mock_success(payload: object) -> ApiSuccess:
    return ApiSuccess(data=payload, request_count=1)


def test_bybit_excluded_catalog_and_clients():
    settings = make_test_settings(market_opportunity_catalog_discovery_enabled=True)
    assert "bybit" not in V43_EXCHANGE_IDS
    assert "bybit" not in build_v43_clients(settings)
    assert "bybit" in EXCHANGE_IDS
    assert "bybit" in build_clients(settings)
    universe = build_symbol_universe(
        settings, selected_exchanges=("binance", "bybit", "okx"), persist=False
    )
    assert any("bybit:excluded" in r for r in universe.rejects)


def test_catalog_common_symbol_intersection(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "u.db"),
        market_opportunity_catalog_discovery_enabled=True,
        crypto_intel_enabled=True,
        crypto_intel_read_only=True,
    )
    initialize_database(settings)
    initialize_market_opportunity_schema(settings)

    def fake_discover(settings_obj, venue_id, **kwargs):
        now = datetime.now(timezone.utc)
        if venue_id == "binance":
            payload = {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    },
                    {
                        "symbol": "ETHUSDT",
                        "baseAsset": "ETH",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    },
                ]
            }
        else:
            # OKX-shaped: only BTC common
            payload = {
                "code": "0",
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "baseCcy": "BTC",
                        "quoteCcy": "USDT",
                        "state": "live",
                    }
                ],
            }
        with patch(
            "app.crypto.catalog_discovery.public_get_json",
            return_value=_mock_success(payload),
        ):
            return discover_spot_catalog(settings_obj, venue_id, now=now)

    with patch(
        "app.crypto.opportunity.universe.discover_spot_catalog",
        side_effect=fake_discover,
    ):
        universe = build_symbol_universe(
            settings,
            selected_exchanges=("binance", "okx"),
            persist=True,
        )
    assert "BTC/USDT" in universe.symbols
    assert "ETH/USDT" not in universe.symbols
    assert any("ETH/USDT:insufficient_venues" in r for r in universe.rejects)


def test_partial_catalog_never_delists(tmp_path):
    db = tmp_path / "partial.db"
    settings = make_test_settings(
        database_path=str(db),
        market_opportunity_catalog_discovery_enabled=True,
        crypto_intel_read_only=True,
    )
    initialize_market_opportunity_schema(settings)
    now = datetime.now(timezone.utc)
    full = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            {
                "symbol": "ETHUSDT",
                "baseAsset": "ETH",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
        ]
    }
    with patch(
        "app.crypto.catalog_discovery.public_get_json",
        return_value=_mock_success(full),
    ):
        first = discover_spot_catalog(settings, "binance", now=now, session_id="s1")
    assert first.completeness is DiscoveryCompleteness.COMPLETE
    persist_catalog_discovery_result(settings, first)

    partial = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            {"symbol": "BAD"},
        ]
    }
    with patch(
        "app.crypto.catalog_discovery.public_get_json",
        return_value=_mock_success(partial),
    ):
        second = discover_spot_catalog(
            settings, "binance", now=now + timedelta(minutes=1), session_id="s2"
        )
    assert second.completeness is DiscoveryCompleteness.PARTIAL
    persist_catalog_discovery_result(settings, second)
    rows = list_catalog_instruments(settings, venue_id="binance")
    by_id = {r["venue_market_id"]: r for r in rows}
    assert by_id["ETHUSDT"]["quality_status"] == "active"


def test_complete_miss_deactivates_absent_only(tmp_path):
    db = tmp_path / "miss.db"
    settings = make_test_settings(
        database_path=str(db),
        market_opportunity_catalog_discovery_enabled=True,
        crypto_intel_read_only=True,
    )
    initialize_market_opportunity_schema(settings)
    now = datetime.now(timezone.utc)
    full = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            {
                "symbol": "ETHUSDT",
                "baseAsset": "ETH",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
        ]
    }
    with patch(
        "app.crypto.catalog_discovery.public_get_json",
        return_value=_mock_success(full),
    ):
        first = discover_spot_catalog(settings, "binance", now=now, session_id="c1")
    persist_catalog_discovery_result(settings, first)

    only_btc = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
            }
        ]
    }
    with patch(
        "app.crypto.catalog_discovery.public_get_json",
        return_value=_mock_success(only_btc),
    ):
        second = discover_spot_catalog(
            settings, "binance", now=now + timedelta(minutes=2), session_id="c2"
        )
    assert second.completeness is DiscoveryCompleteness.COMPLETE
    persist_catalog_discovery_result(settings, second)
    rows = list_catalog_instruments(settings, venue_id="binance")
    by_id = {r["venue_market_id"]: r for r in rows}
    assert by_id["BTCUSDT"]["quality_status"] == "active"
    assert by_id["ETHUSDT"]["quality_status"] == "inactive"


def test_inactive_and_malformed_excluded_from_universe(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "inactive.db"),
        market_opportunity_catalog_discovery_enabled=True,
        crypto_intel_read_only=True,
    )
    initialize_market_opportunity_schema(settings)
    now = datetime.now(timezone.utc)
    payload = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            {
                "symbol": "ETHUSDT",
                "baseAsset": "ETH",
                "quoteAsset": "USDT",
                "status": "BREAK",
            },
            {"symbol": "BAD"},
        ]
    }

    def fake_discover(settings_obj, venue_id, **kwargs):
        with patch(
            "app.crypto.catalog_discovery.public_get_json",
            return_value=_mock_success(payload),
        ):
            return discover_spot_catalog(settings_obj, venue_id, now=now)

    with patch(
        "app.crypto.opportunity.universe.discover_spot_catalog",
        side_effect=fake_discover,
    ):
        # PARTIAL due to BAD row — insufficient complete venues
        universe = build_symbol_universe(
            settings, selected_exchanges=("binance", "okx"), persist=False
        )
    assert universe.symbols == ()
    assert any("discovery_partial" in r for r in universe.rejects) or any(
        "insufficient_complete" in r for r in universe.rejects
    )


def test_executable_fees_slippage_net_roi_and_rejects():
    receive = now_ms()
    books = {
        "binance": _book("binance", ask=100.0, ask_size=5.0, bid=99.0, bid_size=5.0),
        "okx": _book("okx", ask=102.0, ask_size=5.0, bid=101.5, bid_size=5.0),
    }
    buy = allocate_buy_usd(books["binance"], 100.0)
    assert buy.available
    sell = allocate_sell_base(books["okx"], buy.filled_base)
    assert sell.available
    opp, reject = evaluate_cross_venue_opportunity(
        books=books,
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=receive,
    )
    assert reject is None
    assert opp is not None
    assert opp.buy_venue == "binance"
    assert opp.sell_venue == "okx"
    assert opp.trading_fees_usd > 0
    assert opp.net_profit_usd > 0
    assert opp.roi > 0

    stale = {
        "binance": _book("binance", ts_ms=receive - 120_000),
        "okx": _book("okx", bid=110.0, ask=111.0, ts_ms=receive - 120_000),
    }
    opp2, reject2 = evaluate_cross_venue_opportunity(
        books=stale,
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=receive,
    )
    assert opp2 is None
    assert reject2 is not None

    thin = {
        "binance": _book("binance", ask_size=0.0001, bid_size=0.0001),
        "okx": _book("okx", bid=110.0, ask=111.0, bid_size=0.0001, ask_size=0.0001),
    }
    opp3, reject3 = evaluate_cross_venue_opportunity(
        books=thin,
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=receive,
    )
    assert opp3 is None
    assert reject3 is not None


def test_contradictory_symbol_fail_closed():
    receive = now_ms()
    btc = _book("binance", symbol="BTC/USDT")
    eth = _book("okx", symbol="ETH/USDT", bid=110.0, ask=111.0)
    opp, reject = evaluate_cross_venue_opportunity(
        books={"binance": btc, "okx": eth},
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=receive,
    )
    assert opp is None
    assert reject is not None
    assert reject.code == "incompatible_instruments"


def test_per_user_venue_isolation_in_universe(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "iso.db"),
        market_opportunity_catalog_discovery_enabled=False,
        crypto_symbols=["BTC/USDT"],
    )
    user_a = build_symbol_universe(
        settings, selected_exchanges=("binance", "okx"), persist=False
    )
    user_b = build_symbol_universe(
        settings, selected_exchanges=("binance",), persist=False
    )
    assert "BTC/USDT" in user_a.symbols
    assert user_b.symbols == ()
    assert any("insufficient_venues" in r for r in user_b.rejects)


def test_card_formatting_required_fields():
    opp = PaperNetOpportunity(
        opportunity_key="k",
        opportunity_uid="u",
        pair="BTC/USDT",
        product_type="spot",
        buy_venue="binance",
        sell_venue="okx",
        buy_ask=100.0,
        sell_bid=101.0,
        buy_vwap=100.1,
        sell_vwap=100.9,
        gross_spread_usd=0.8,
        buy_fee_usd=0.1,
        sell_fee_usd=0.1,
        trading_fees_usd=0.2,
        estimated_slippage=0.01,
        paper_allocation_usd=Decimal("100"),
        executable_base=1.0,
        executable_liquidity_usd=500.0,
        net_profit_usd=0.5,
        roi=0.005,
        signal_age_seconds=1.0,
        checked_at=datetime.now(timezone.utc),
    )
    text = format_crypto_opportunity_card(opp)
    for needle in (
        "BUY venue:",
        "SELL venue:",
        "Symbol:",
        "Executable quantity:",
        "Buy executable price:",
        "Sell executable price:",
        "Trading fees:",
        "Estimated slippage:",
        "Net paper profit:",
        "ROI:",
        "Freshness",
        "paper-only",
        "Not guaranteed",
    ):
        assert needle in text
    lower = text.lower()
    for banned in ("open long", "take-profit", "perpetual", "funding rate", "news-based"):
        assert banned not in lower


def test_scanner_and_delivery_flags_default_unchanged():
    settings = make_test_settings()
    assert settings.crypto_opportunity_scanner_enabled is False
    assert settings.crypto_opportunity_delivery_enabled is False
    assert settings.market_opportunity_catalog_discovery_enabled is False
    report = scan_crypto_opportunities(
        settings, user_settings=balanced_crypto_settings()
    )
    assert report.enabled is False


def test_no_trading_symbols_in_v44_modules():
    from pathlib import Path

    for path in (
        "app/crypto/opportunity/universe.py",
        "app/crypto/opportunity/scanner.py",
        "app/crypto/opportunity/netting.py",
        "app/crypto/catalog_discovery.py",
    ):
        text = Path(path).read_text(encoding="utf-8").lower()
        for forbidden in (
            "place_order",
            "withdraw",
            "private_key",
            "api_secret",
            "transfer",
            "balance",
        ):
            assert forbidden not in text, path
