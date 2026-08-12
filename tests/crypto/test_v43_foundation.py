"""Focused offline tests for V4.3 Slice 1 registry + Slice 2 catalog discovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.catalog_discovery import (
    catalog_discovery_enabled,
    discover_catalog,
    discover_spot_catalog,
)
from app.crypto.discovery_models import DiscoveryCompleteness, DiscoveryDiagnosticCode
from app.crypto.exchanges import build_clients
from app.crypto.models import EXCHANGE_IDS
from app.crypto.product_types import ProductType
from app.crypto.venue_registry import (
    V43_EXCHANGE_IDS,
    CanonicalInstrument,
    get_v43_venue,
    is_v43_venue,
    iter_v43_venues,
)
from app.database.market_opportunity_storage import (
    CATALOG_MIGRATION_ID,
    initialize_market_opportunity_schema,
    is_catalog_migration_applied,
    list_catalog_instruments,
    persist_catalog_discovery_result,
)
from app.database.storage import get_connection
from tests.conftest import make_test_settings


def _enabled_settings(**overrides):
    return make_test_settings(
        market_opportunity_catalog_discovery_enabled=True,
        **overrides,
    )


def test_product_types_and_canonical_identity():
    assert {p.value for p in ProductType} == {
        "spot",
        "perpetual",
        "dated_future",
        "unsupported",
    }
    inst = CanonicalInstrument(
        venue_id="binance",
        product_type=ProductType.SPOT,
        venue_market_id="BTCUSDT",
        base="BTC",
        quote="USDT",
        normalized_symbol="BTC/USDT",
    )
    assert inst.identity_key == ("binance", "spot", "BTCUSDT")
    with pytest.raises(ValueError):
        CanonicalInstrument(
            venue_id="binance",
            product_type=ProductType.SPOT,
            venue_market_id="BTCUSDT",
            base="BTC",
            quote="USDT",
            normalized_symbol="BTC/USDT",
            contract_expiry="2026-12-25",
        )


def test_v43_registry_excludes_bybit_preserves_v3():
    assert V43_EXCHANGE_IDS == (
        "binance",
        "bingx",
        "mexc",
        "okx",
        "kucoin",
    )
    assert "bybit" not in V43_EXCHANGE_IDS
    assert is_v43_venue("bybit") is False
    assert get_v43_venue("bybit") is None
    assert [v.venue_id for v in iter_v43_venues()] == list(V43_EXCHANGE_IDS)
    assert "bybit" in EXCHANGE_IDS
    clients = build_clients(make_test_settings())
    assert "bybit" in clients
    assert clients["bybit"].exchange_id == "bybit"


def test_unsupported_products_are_not_venue_failures():
    venue = get_v43_venue("okx")
    assert venue is not None
    perp = venue.product(ProductType.PERPETUAL)
    assert perp.supported is False
    assert perp.resolved_product_type is ProductType.UNSUPPORTED
    spot = venue.product(ProductType.SPOT)
    assert spot.supported is True
    assert spot.capabilities is not None
    assert spot.capabilities.market_catalog is True
    assert spot.capabilities.ticker is True
    assert spot.capabilities.order_book is True


def test_feature_flag_defaults_off_no_http():
    settings = make_test_settings()
    assert settings.market_opportunity_catalog_discovery_enabled is False
    assert catalog_discovery_enabled(settings) is False
    with patch("app.crypto.catalog_discovery.public_get_json") as mocked:
        result = discover_spot_catalog(settings, "binance")
        mocked.assert_not_called()
    assert result.completeness is DiscoveryCompleteness.FAILED
    assert result.diagnostics[0].code is DiscoveryDiagnosticCode.FEATURE_DISABLED


def test_unsupported_product_family_no_http():
    settings = _enabled_settings()
    with patch("app.crypto.catalog_discovery.public_get_json") as mocked:
        result = discover_catalog(settings, "binance", ProductType.PERPETUAL)
        mocked.assert_not_called()
    assert result.completeness is DiscoveryCompleteness.UNSUPPORTED
    assert result.diagnostics[0].code is DiscoveryDiagnosticCode.UNSUPPORTED_PRODUCT


def _mock_success(payload, request_count: int = 1):
    return ApiSuccess(data=payload, request_count=request_count)


@pytest.mark.parametrize(
    ("venue_id", "payload", "expected_market_id", "expected_symbol"),
    [
        (
            "binance",
            {
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
            },
            "BTCUSDT",
            "BTC/USDT",
        ),
        (
            "bingx",
            {
                "code": 0,
                "data": {
                    "symbols": [
                        {"symbol": "BTC-USDT", "status": 1},
                        {"symbol": "ETH-USDT", "status": 0},
                        {"symbol": "INVALID"},
                    ]
                },
            },
            "BTC-USDT",
            "BTC/USDT",
        ),
        (
            "mexc",
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "1",
                        "isSpotTradingAllowed": True,
                    },
                    {
                        "symbol": "ETHUSDT",
                        "baseAsset": "ETH",
                        "quoteAsset": "USDT",
                        "status": "1",
                        "isSpotTradingAllowed": False,
                    },
                    {"not_a_symbol": True},
                ]
            },
            "BTCUSDT",
            "BTC/USDT",
        ),
        (
            "okx",
            {
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "baseCcy": "BTC",
                        "quoteCcy": "USDT",
                        "state": "live",
                    },
                    {
                        "instId": "ETH-USDT",
                        "baseCcy": "ETH",
                        "quoteCcy": "USDT",
                        "state": "suspend",
                    },
                    {"instId": "BAD"},
                ]
            },
            "BTC-USDT",
            "BTC/USDT",
        ),
        (
            "kucoin",
            {
                "data": [
                    {
                        "symbol": "BTC-USDT",
                        "baseCurrency": "BTC",
                        "quoteCurrency": "USDT",
                        "enableTrading": True,
                    },
                    {
                        "symbol": "ETH-USDT",
                        "baseCurrency": "ETH",
                        "quoteCurrency": "USDT",
                        "enableTrading": False,
                    },
                    {"symbol": "X"},
                ]
            },
            "BTC-USDT",
            "BTC/USDT",
        ),
    ],
)
def test_five_venues_normalize_dedupe_partial_not_complete(
    venue_id, payload, expected_market_id, expected_symbol
):
    settings = _enabled_settings()
    if venue_id == "binance":
        payload = {
            "symbols": list(payload["symbols"]) + [dict(payload["symbols"][0])]
        }
    elif venue_id == "bingx":
        payload = {
            "code": 0,
            "data": {
                "symbols": list(payload["data"]["symbols"])
                + [dict(payload["data"]["symbols"][0])]
            },
        }
    elif venue_id == "mexc":
        payload = {"symbols": list(payload["symbols"]) + [dict(payload["symbols"][0])]}
    elif venue_id == "okx":
        payload = {"data": list(payload["data"]) + [dict(payload["data"][0])]}
    else:
        payload = {"data": list(payload["data"]) + [dict(payload["data"][0])]}

    with patch(
        "app.crypto.catalog_discovery.public_get_json",
        return_value=_mock_success(payload),
    ) as mocked:
        result = discover_spot_catalog(settings, venue_id)
        mocked.assert_called_once()
        args, _kwargs = mocked.call_args
        assert isinstance(args[1], str)
        assert args[1].startswith("http")

    assert result.venue_id == venue_id
    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert result.pagination.exhausted is False
    market_ids = [row.instrument.venue_market_id for row in result.instruments]
    assert expected_market_id in market_ids
    assert len(market_ids) == len(set(market_ids))
    match = next(
        row
        for row in result.instruments
        if row.instrument.venue_market_id == expected_market_id
    )
    assert match.instrument.normalized_symbol == expected_symbol
    assert match.instrument.identity_key == (
        venue_id,
        "spot",
        expected_market_id,
    )
    assert any(
        d.code
        in {
            DiscoveryDiagnosticCode.MALFORMED_ROW,
            DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
            DiscoveryDiagnosticCode.DUPLICATE_INSTRUMENT,
        }
        for d in result.diagnostics
    )


def test_complete_when_all_rows_valid():
    settings = _enabled_settings()
    payload = {
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
        return_value=_mock_success(payload),
    ):
        result = discover_spot_catalog(settings, "binance")
    assert result.completeness is DiscoveryCompleteness.COMPLETE
    assert result.pagination.exhausted is True
    assert result.pagination.stopped_reason is None


def test_http_failure_is_failed_not_complete():
    settings = _enabled_settings()
    failure = ApiFailure(
        code="http_status",
        status_code=429,
        retryable=True,
        request_count=1,
        message="HTTP 429",
    )
    with patch(
        "app.crypto.catalog_discovery.public_get_json",
        return_value=failure,
    ):
        result = discover_spot_catalog(settings, "binance")
    assert result.completeness is DiscoveryCompleteness.FAILED
    assert result.diagnostics[0].code is DiscoveryDiagnosticCode.RATE_LIMITED
    assert result.instruments == ()


def test_catalog_persistence_additive_idempotent_upsert_no_mass_delete(tmp_path):
    db_path = tmp_path / "v43_catalog.db"
    settings = make_test_settings(database_path=str(db_path))
    initialize_market_opportunity_schema(settings)
    assert is_catalog_migration_applied(settings)
    initialize_market_opportunity_schema(settings)
    assert is_catalog_migration_applied(settings)

    storage_text = Path("app/database/market_opportunity_storage.py").read_text(
        encoding="utf-8"
    )
    assert "DROP TABLE" not in storage_text
    assert "DROP INDEX" not in storage_text

    now = datetime.now(timezone.utc)
    settings_on = _enabled_settings(database_path=str(db_path))
    payload_full = {
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
        return_value=_mock_success(payload_full),
    ):
        first = discover_spot_catalog(
            settings_on, "binance", now=now, session_id="sess-1"
        )
    persist_catalog_discovery_result(settings_on, first)
    rows = list_catalog_instruments(settings_on, venue_id="binance")
    assert {r["venue_market_id"] for r in rows} == {"BTCUSDT", "ETHUSDT"}
    eth_before = next(r for r in rows if r["venue_market_id"] == "ETHUSDT")

    later = now + timedelta(minutes=5)
    payload_partial = {
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
        return_value=_mock_success(payload_partial),
    ):
        second = discover_spot_catalog(
            settings_on, "binance", now=later, session_id="sess-2"
        )
    assert second.completeness is DiscoveryCompleteness.PARTIAL
    persist_catalog_discovery_result(settings_on, second)
    rows_after = list_catalog_instruments(settings_on, venue_id="binance")
    by_id = {r["venue_market_id"]: r for r in rows_after}
    assert "ETHUSDT" in by_id
    assert by_id["ETHUSDT"]["quality_status"] == eth_before["quality_status"]
    assert by_id["BTCUSDT"]["last_seen_at"] >= by_id["ETHUSDT"]["last_seen_at"]
    assert by_id["BTCUSDT"]["discovery_session_id"] == "sess-2"

    with get_connection(settings_on) as connection:
        markers = {
            row["migration_id"]
            for row in connection.execute(
                "SELECT migration_id FROM moi_schema_migrations"
            ).fetchall()
        }
    assert "market_opportunity_v1" in markers
    assert CATALOG_MIGRATION_ID in markers


def test_discovery_module_has_no_auth_or_trading_symbols():
    text = Path("app/crypto/catalog_discovery.py").read_text(encoding="utf-8").lower()
    for forbidden in (
        "signature",
        "api_key",
        "authorization",
        "x-mbx",
        "private_key",
        "place_order",
        "withdraw",
    ):
        assert forbidden not in text
