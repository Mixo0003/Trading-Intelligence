"""Focused offline tests for V4.3 Crypto Opportunity Scanner."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.exchanges import build_clients, build_v43_clients
from app.crypto.freshness import now_ms
from app.crypto.models import BookLevel, EXCHANGE_IDS, Instrument, OrderBookSnapshot
from app.crypto.opportunity.catalog import load_top_crypto_opportunities
from app.crypto.opportunity.delivery import (
    recheck_delivery_authorization,
    select_crypto_delivery_wave,
)
from app.crypto.opportunity.filters import qualify_for_user
from app.crypto.opportunity.formatting import format_crypto_opportunity_card
from app.crypto.opportunity.identity import build_opportunity_key
from app.crypto.opportunity.netting import evaluate_cross_venue_opportunity
from app.crypto.opportunity.scanner import scan_crypto_opportunities
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    CryptoOpportunitySubscription,
    balanced_crypto_settings,
)
from app.crypto.opportunity.universe import build_symbol_universe
from app.crypto.order_book import (
    allocate_buy_usd,
    allocate_sell_base,
    build_order_book_snapshot,
)
from app.crypto.product_types import ProductType
from app.crypto.venue_registry import V43_EXCHANGE_IDS, get_v43_venue
from app.database import crypto_opportunity_storage as storage
from app.database.storage import initialize_database, upsert_user
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


def test_v43_clients_exclude_bybit_preserve_v3():
    settings = make_test_settings()
    v43 = build_v43_clients(settings)
    assert set(v43) == set(V43_EXCHANGE_IDS)
    assert "bybit" not in v43
    assert "bybit" in EXCHANGE_IDS
    assert "bybit" in build_clients(settings)
    venue = get_v43_venue("binance")
    assert venue is not None
    assert venue.product(ProductType.SPOT).capabilities.order_book is True


def test_order_book_normalize_and_malformed():
    inst = _inst()
    ok = build_order_book_snapshot(
        exchange_id="binance",
        instrument=inst,
        bids_raw=[["100", "2"]],
        asks_raw=[["101", "2"]],
        timestamp_ms=None,
        allow_receive_time=True,
    )
    assert isinstance(ok, ApiSuccess)
    bad = build_order_book_snapshot(
        exchange_id="binance",
        instrument=inst,
        bids_raw=[["x", "2"]],
        asks_raw=[["101", "2"]],
        timestamp_ms=None,
        allow_receive_time=True,
    )
    assert isinstance(bad, ApiFailure)
    assert bad.code == "malformed_order_book"


def test_stale_order_book_rejected():
    inst = _inst()
    old = now_ms() - 120_000
    result = build_order_book_snapshot(
        exchange_id="okx",
        instrument=inst,
        bids_raw=[["100", "2"]],
        asks_raw=[["101", "2"]],
        timestamp_ms=old,
        allow_receive_time=False,
        max_age_seconds=30.0,
    )
    assert isinstance(result, ApiFailure)


def test_allocation_slippage_and_insufficient_depth():
    book = _book("binance", ask=100.0, ask_size=0.1, bid=99.0, bid_size=0.1)
    buy = allocate_buy_usd(book, 100.0)
    assert buy.available is False
    assert buy.unavailable_code == "insufficient_depth"
    deep = _book("binance", ask=100.0, ask_size=5.0, bid=99.5, bid_size=5.0)
    buy_ok = allocate_buy_usd(deep, 100.0)
    assert buy_ok.available is True
    sell = allocate_sell_base(deep, buy_ok.filled_base or 0.0)
    assert sell.available is True


def test_missing_fee_data_fail_closed(monkeypatch):
    books = {
        "binance": _book("binance", ask=100.0, bid=99.0),
        "okx": _book("okx", ask=102.0, bid=101.5, bid_size=5.0, ask_size=5.0),
    }

    def _missing(exchange_id, **kwargs):
        return ApiFailure(
            code="missing_fee_schedule",
            status_code=None,
            retryable=False,
            request_count=0,
            message="missing",
        )

    monkeypatch.setattr(
        "app.crypto.opportunity.netting.get_spot_fee_schedule", _missing
    )
    opp, reject = evaluate_cross_venue_opportunity(
        books=books,
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=now_ms(),
    )
    assert opp is None
    assert reject is not None
    assert reject.code == "missing_fee_schedule"


def test_net_profit_roi_and_ranking():
    # Buy cheap on binance, sell rich on okx.
    books = {
        "binance": _book(
            "binance", ask=100.0, ask_size=5.0, bid=99.0, bid_size=5.0
        ),
        "okx": _book("okx", ask=110.0, ask_size=5.0, bid=109.0, bid_size=5.0),
    }
    opp, reject = evaluate_cross_venue_opportunity(
        books=books,
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=now_ms(),
    )
    assert reject is None
    assert opp is not None
    assert opp.buy_venue == "binance"
    assert opp.sell_venue == "okx"
    assert opp.net_profit_usd > 0
    assert opp.roi > 0
    assert opp.trading_fees_usd > 0
    assert opp.opportunity_key.startswith("crypto_v1:")


def test_universe_allowlist_and_partial_catalog():
    settings = make_test_settings(
        crypto_symbols=["BTC/USDT"],
        market_opportunity_catalog_discovery_enabled=False,
    )
    result = build_symbol_universe(
        settings, selected_exchanges=("binance", "okx")
    )
    assert "BTC/USDT" in result.symbols
    assert result.completeness_by_venue["binance"] == "allowlist"

    settings_on = make_test_settings(
        market_opportunity_catalog_discovery_enabled=True,
        crypto_intel_read_only=True,
    )
    with patch(
        "app.crypto.opportunity.universe.discover_spot_catalog"
    ) as mocked:
        from app.crypto.discovery_models import (
            CatalogDiscoveryResult,
            DiscoveryCompleteness,
            DiscoveryProvenance,
            PaginationState,
        )

        now = datetime.now(timezone.utc)
        mocked.return_value = CatalogDiscoveryResult(
            session_id="sess-partial",
            venue_id="binance",
            product_family=ProductType.SPOT,
            completeness=DiscoveryCompleteness.PARTIAL,
            instruments=(),
            diagnostics=(),
            provenance=DiscoveryProvenance(
                source_id="t",
                endpoint_path="/x",
                request_count=1,
                fetched_at=now,
            ),
            pagination=PaginationState(
                mode="none",
                pages_fetched=1,
                exhausted=False,
                next_cursor=None,
                stopped_reason="row_errors",
            ),
            started_at=now,
            finished_at=now,
        )
        partial = build_symbol_universe(
            settings_on, selected_exchanges=("binance",), persist=False
        )
    assert partial.completeness_by_venue["binance"] == "partial"
    assert any("partial" in r for r in partial.rejects)


def test_user_filters_cooldown_isolation(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "cos.db"))
    initialize_database(settings)
    upsert_user(settings, 101, "a", "A")
    upsert_user(settings, 202, "b", "B")
    profile_a = storage.ensure_profile(settings, 101)
    profile_b = storage.get_or_default_settings(settings, 202)
    assert profile_a.delivery_enabled is False
    assert profile_b.delivery_enabled is False

    opp_books = {
        "binance": _book("binance", ask=100.0, ask_size=5.0, bid=99.0, bid_size=5.0),
        "okx": _book("okx", ask=110.0, ask_size=5.0, bid=109.0, bid_size=5.0),
    }
    opp, _ = evaluate_cross_venue_opportunity(
        books=opp_books,
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=now_ms(),
    )
    assert opp is not None
    storage.record_sent(
        settings,
        telegram_id=101,
        opportunity_key=opp.opportunity_key,
        opportunity_uid=opp.opportunity_uid,
    )
    ok_a, reason_a = qualify_for_user(
        opp,
        profile_a,
        last_sent_at=storage.last_sent_at_for_key(
            settings, telegram_id=101, opportunity_key=opp.opportunity_key
        ),
        require_delivery_enabled=False,
        apply_cooldown=True,
    )
    assert ok_a is False
    assert reason_a == "cooldown_active"
    ok_b, reason_b = qualify_for_user(
        opp,
        profile_b,
        last_sent_at=storage.last_sent_at_for_key(
            settings, telegram_id=202, opportunity_key=opp.opportunity_key
        ),
        require_delivery_enabled=False,
        apply_cooldown=True,
    )
    assert ok_b is True
    assert reason_b is None


def test_telegram_card_no_raw_payload():
    books = {
        "binance": _book("binance", ask=100.0, ask_size=5.0, bid=99.0, bid_size=5.0),
        "mexc": _book("mexc", ask=108.0, ask_size=5.0, bid=107.0, bid_size=5.0),
    }
    opp, _ = evaluate_cross_venue_opportunity(
        books=books,
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=now_ms(),
    )
    assert opp is not None
    text = format_crypto_opportunity_card(opp)
    assert text.startswith("CRYPTO OPPORTUNITY")
    assert "Symbol:" in text or "Pair:" in text
    assert "BUY venue:" in text or "Buy venue" in text
    assert "Public read-only / paper-only" in text
    assert "{" not in text
    assert "bids" not in text
    assert "asks" not in text


def test_delivery_wave_dedupe_and_global_gate(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "del.db"),
        crypto_opportunity_delivery_enabled=False,
    )
    initialize_database(settings)
    upsert_user(settings, 7, "u", "U")
    user = CryptoOpportunitySettings(
        selected_exchanges=V43_EXCHANGE_IDS,
        selected_product_types=("spot",),
        paper_allocation_usd=Decimal("100"),
        minimum_net_profit_usd=Decimal("0"),
        minimum_roi=Decimal("0"),
        maximum_slippage=Decimal("0.02"),
        minimum_executable_depth_usd=Decimal("100"),
        maximum_signal_age_seconds=30,
        delivery_enabled=True,
        cooldown_seconds=1800,
    )
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(telegram_id=7, settings=user),
    )
    books = {
        "binance": _book("binance", ask=100.0, ask_size=5.0, bid=99.0, bid_size=5.0),
        "okx": _book("okx", ask=110.0, ask_size=5.0, bid=109.0, bid_size=5.0),
    }
    opp, _ = evaluate_cross_venue_opportunity(
        books=books,
        paper_allocation_usd=Decimal("100"),
        maximum_slippage=Decimal("0.05"),
        maximum_signal_age_seconds=30.0,
        receive_ms=now_ms(),
    )
    assert opp is not None
    wave = select_crypto_delivery_wave(
        settings, opp, recipients=[(7, user)]
    )
    assert wave.pending_count == 1
    wave2 = select_crypto_delivery_wave(
        settings, opp, recipients=[(7, user)]
    )
    assert wave2.ignored_duplicates == 1
    ok, reason = recheck_delivery_authorization(
        settings, telegram_id=7, opportunity_key=opp.opportunity_key
    )
    assert ok is False
    assert reason == "global_delivery_disabled"


def test_scanner_mocked_books_no_http(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "scan.db"),
        crypto_intel_enabled=True,
        crypto_opportunity_scanner_enabled=True,
        crypto_symbols=["BTC/USDT"],
        market_opportunity_catalog_discovery_enabled=False,
    )
    initialize_database(settings)
    upsert_user(settings, 1, "t", "T")

    clients = {}
    for venue in ("binance", "okx"):
        mock = MagicMock()
        bid = 99.0 if venue == "binance" else 109.0
        ask = 100.0 if venue == "binance" else 110.0
        mock.fetch_order_book.return_value = ApiSuccess(
            data=_book(venue, ask=ask, ask_size=5.0, bid=bid, bid_size=5.0)
        )
        clients[venue] = mock

    user = CryptoOpportunitySettings(
        selected_exchanges=("binance", "okx"),
        paper_allocation_usd=Decimal("100"),
        minimum_executable_depth_usd=Decimal("100"),
    )
    with patch(
        "app.crypto.opportunity.scanner.build_v43_clients", return_value=clients
    ):
        report = scan_crypto_opportunities(settings, user_settings=user)
        top = load_top_crypto_opportunities(
            settings, telegram_id=1, user_settings=user
        )
    assert report.enabled is True
    assert len(report.opportunities) >= 1
    assert top.enabled is True
    for client in clients.values():
        assert client.fetch_order_book.called


def test_identity_stable():
    key = build_opportunity_key(
        pair="BTC/USDT",
        buy_venue="binance",
        sell_venue="okx",
        product_type="spot",
    )
    assert key == "crypto_v1:BTC/USDT:binance:okx:spot:buy_low_sell_high"


def test_feature_flags_default_off():
    settings = make_test_settings()
    assert settings.crypto_opportunity_scanner_enabled is False
    assert settings.crypto_opportunity_delivery_enabled is False
    report = scan_crypto_opportunities(
        make_test_settings(crypto_intel_enabled=True),
        user_settings=balanced_crypto_settings(),
    )
    assert report.enabled is False
