"""Focused offline tests for V4.5 futures / basis / funding foundation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.crypto.catalog_discovery import discover_catalog
from app.crypto.discovery_models import DiscoveryCompleteness, DiscoveryDiagnosticCode
from app.crypto.exchanges import build_clients, build_v43_clients
from app.crypto.market_snapshots import (
    NormalizedMarketSnapshot,
    SnapshotCapabilityPresence,
    SnapshotProvenance,
)
from app.crypto.models import EXCHANGE_IDS
from app.crypto.opportunity.basis_funding import (
    BasisObservation,
    FoundationRejectCode,
    FundingObservation,
    evaluate_basis_opportunity,
    evaluate_funding_opportunity,
    format_foundation_status_card,
)
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    CryptoOpportunitySubscription,
)
from app.crypto.product_types import PRODUCT_FAMILIES, ProductType
from app.crypto.venue_registry import (
    V43_EXCHANGE_IDS,
    CanonicalInstrument,
    get_v43_venue,
    iter_v43_venues,
)
from app.database import crypto_opportunity_storage as storage
from app.database.storage import initialize_database, upsert_user
from tests.conftest import make_test_settings


def _now() -> datetime:
    return datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _inst(
    *,
    venue_id: str = "binance",
    product_type: ProductType = ProductType.SPOT,
    venue_market_id: str = "BTCUSDT",
    contract_expiry: str | None = None,
    settlement: str | None = None,
    tick_size: str | None = None,
    quantity_step: str | None = None,
    contract_code: str | None = None,
) -> CanonicalInstrument:
    return CanonicalInstrument(
        venue_id=venue_id,
        product_type=product_type,
        venue_market_id=venue_market_id,
        base="BTC",
        quote="USDT",
        normalized_symbol="BTC/USDT",
        contract_expiry=contract_expiry,
        settlement=settlement,
        tick_size=tick_size,
        quantity_step=quantity_step,
        contract_code=contract_code,
    )


def _provenance(*, observed_at: datetime | None = None) -> SnapshotProvenance:
    return SnapshotProvenance(
        source_id="test_fixture",
        endpoint_path="",
        observed_at=observed_at or _now(),
        request_count=0,
        note="offline fixture",
    )


def _spot_snapshot(**overrides) -> NormalizedMarketSnapshot:
    payload = {
        "instrument": _inst(),
        "product_type": ProductType.SPOT,
        "presence": SnapshotCapabilityPresence(bid_ask=True),
        "provenance": _provenance(),
        "as_of": _now(),
        "best_bid": Decimal("100"),
        "best_ask": Decimal("101"),
    }
    payload.update(overrides)
    return NormalizedMarketSnapshot(**payload)


def _perp_snapshot(**overrides) -> NormalizedMarketSnapshot:
    payload = {
        "instrument": _inst(
            product_type=ProductType.PERPETUAL,
            venue_market_id="BTCUSDT-PERP",
        ),
        "product_type": ProductType.PERPETUAL,
        "presence": SnapshotCapabilityPresence(
            bid_ask=True, mark_price=True, funding=True
        ),
        "provenance": _provenance(),
        "as_of": _now(),
        "best_bid": Decimal("100"),
        "best_ask": Decimal("101"),
        "mark_price": Decimal("100.5"),
        "funding_rate": Decimal("0.0001"),
        "funding_time": _now(),
    }
    payload.update(overrides)
    return NormalizedMarketSnapshot(**payload)


def test_product_type_registry_spot_supported_futures_unsupported():
    assert PRODUCT_FAMILIES == frozenset(
        {ProductType.SPOT, ProductType.PERPETUAL, ProductType.DATED_FUTURE}
    )
    for venue in iter_v43_venues():
        assert venue.supports(ProductType.SPOT) is True
        assert venue.supported_product_families() == (ProductType.SPOT,)
        assert venue.unsupported_product_families() == (
            ProductType.PERPETUAL,
            ProductType.DATED_FUTURE,
        )
        spot = venue.product(ProductType.SPOT)
        assert spot.capabilities is not None
        assert spot.capabilities.funding is False
        assert spot.capabilities.open_interest is False
        perp = venue.product(ProductType.PERPETUAL)
        assert perp.supported is False
        assert perp.resolved_product_type is ProductType.UNSUPPORTED
        dated = venue.product(ProductType.DATED_FUTURE)
        assert dated.supported is False
        assert dated.capabilities is None


def test_bybit_excluded_from_v45_registry():
    assert "bybit" not in V43_EXCHANGE_IDS
    assert get_v43_venue("bybit") is None
    assert "bybit" in EXCHANGE_IDS
    assert "bybit" in build_clients(make_test_settings())
    assert "bybit" not in build_v43_clients(make_test_settings())


def test_canonical_instrument_constraints_and_contradictions():
    dated = _inst(
        product_type=ProductType.DATED_FUTURE,
        venue_market_id="BTC-USDT-260925",
        contract_expiry="2026-09-25",
        settlement="USDT",
        tick_size="0.1",
        quantity_step="0.001",
    )
    assert dated.contract_expiry == "2026-09-25"
    assert dated.tick_size == "0.1"
    with pytest.raises(ValueError):
        _inst(product_type=ProductType.SPOT, contract_expiry="2026-09-25")
    with pytest.raises(ValueError):
        _inst(product_type=ProductType.DATED_FUTURE, venue_market_id="X")
    with pytest.raises(ValueError):
        _inst(product_type=ProductType.SPOT, contract_code="BTCUSDT")


def test_unsupported_futures_discovery_no_http():
    settings = make_test_settings(market_opportunity_catalog_discovery_enabled=True)
    with patch("app.crypto.catalog_discovery.public_get_json") as mocked:
        perp = discover_catalog(settings, "binance", ProductType.PERPETUAL)
        dated = discover_catalog(settings, "okx", ProductType.DATED_FUTURE)
        bybit = discover_catalog(settings, "bybit", ProductType.SPOT)
        mocked.assert_not_called()
    assert perp.completeness is DiscoveryCompleteness.UNSUPPORTED
    assert dated.completeness is DiscoveryCompleteness.UNSUPPORTED
    assert bybit.completeness is DiscoveryCompleteness.UNSUPPORTED
    assert perp.diagnostics[0].code is DiscoveryDiagnosticCode.UNSUPPORTED_PRODUCT


def test_snapshot_normalization_and_fail_closed():
    ok = _spot_snapshot()
    assert ok.age_seconds() == 0.0
    assert ok.presence.funding is False
    with pytest.raises(ValueError, match="best_bid must not exceed best_ask"):
        _spot_snapshot(best_bid=Decimal("102"), best_ask=Decimal("101"))
    with pytest.raises(ValueError, match="bid_ask capability requires"):
        _spot_snapshot(
            presence=SnapshotCapabilityPresence(bid_ask=True),
            best_bid=None,
            best_ask=Decimal("101"),
        )
    with pytest.raises(ValueError, match="must be absent when bid_ask"):
        _spot_snapshot(
            presence=SnapshotCapabilityPresence(bid_ask=False),
            best_bid=Decimal("100"),
            best_ask=Decimal("101"),
        )
    with pytest.raises(ValueError, match="must not claim funding"):
        _spot_snapshot(
            presence=SnapshotCapabilityPresence(bid_ask=True, funding=True),
            funding_rate=Decimal("0.01"),
            funding_time=_now(),
        )
    with pytest.raises(ValueError, match="observed_at must not be after as_of"):
        _spot_snapshot(
            provenance=_provenance(
                observed_at=datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc)
            )
        )
    with pytest.raises(ValueError, match="contradicts instrument"):
        _perp_snapshot(
            instrument=_inst(
                product_type=ProductType.DATED_FUTURE,
                venue_market_id="BTC-USDT-260925",
                contract_expiry="2026-09-25",
            ),
            product_type=ProductType.DATED_FUTURE,
            presence=SnapshotCapabilityPresence(bid_ask=True),
            funding_rate=None,
            funding_time=None,
            mark_price=None,
            contract_expiry="2026-12-25",
            best_bid=Decimal("100"),
            best_ask=Decimal("101"),
        )


def test_basis_and_funding_contracts_fail_closed_without_policies():
    spot = _spot_snapshot()
    perp = _perp_snapshot(
        instrument=_inst(
            venue_id="okx",
            product_type=ProductType.PERPETUAL,
            venue_market_id="BTC-USDT-SWAP",
        ),
        mark_price=Decimal("101"),
    )
    basis = BasisObservation(
        left=spot,
        right=perp,
        relationship="spot_perpetual",
        as_of=_now(),
    )
    assert basis.raw_basis == Decimal("0.5")
    opp, reject = evaluate_basis_opportunity(basis)
    assert opp is None
    assert reject.code is FoundationRejectCode.MISSING_FEE_POLICY

    funding = FundingObservation(
        instrument=perp.instrument,
        snapshot=perp,
        as_of=_now(),
    )
    f_opp, f_reject = evaluate_funding_opportunity(funding)
    assert f_opp is None
    assert f_reject.code is FoundationRejectCode.MISSING_FUNDING_FORMULA

    card = format_foundation_status_card(
        title="Funding foundation",
        reject=f_reject,
    )
    assert "unsupported" in card.lower()
    assert "not guaranteed profit" in card.lower()
    assert "long/short" in card.lower()
    for banned in ("take-profit", "news-based", "guaranteed profit of"):
        assert banned not in card.lower()


def test_basis_rejects_malformed_relationships():
    spot = _spot_snapshot()
    other_spot = _spot_snapshot(
        instrument=_inst(venue_id="okx", venue_market_id="BTCUSDT"),
    )
    with pytest.raises(ValueError, match="one spot and one perpetual"):
        BasisObservation(
            left=spot,
            right=other_spot,
            relationship="spot_perpetual",
            as_of=_now(),
        )
    perp_a = _perp_snapshot()
    perp_b = _perp_snapshot(
        instrument=_inst(
            venue_id="binance",
            product_type=ProductType.PERPETUAL,
            venue_market_id="BTCUSDT-PERP-2",
        )
    )
    with pytest.raises(ValueError, match="same-venue perpetual"):
        BasisObservation(
            left=perp_a,
            right=perp_b,
            relationship="futures_futures",
            as_of=_now(),
        )


def test_per_user_isolation_and_spot_settings_unchanged(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "v45.db"))
    initialize_database(settings)
    storage.initialize_crypto_opportunity_schema(settings)
    upsert_user(settings, 101, "a", "A")
    upsert_user(settings, 202, "b", "B")
    a = CryptoOpportunitySettings(
        revision=1,
        selected_exchanges=("binance", "okx"),
        selected_product_types=("spot",),
    )
    b = CryptoOpportunitySettings(
        revision=1,
        selected_exchanges=("mexc", "kucoin"),
        selected_product_types=("spot",),
    )
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(telegram_id=101, settings=a),
    )
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(telegram_id=202, settings=b),
    )
    loaded_a = storage.get_subscription(settings, 101)
    loaded_b = storage.get_subscription(settings, 202)
    assert loaded_a is not None and loaded_b is not None
    assert loaded_a.settings.selected_exchanges == ("binance", "okx")
    assert loaded_b.settings.selected_exchanges == ("mexc", "kucoin")
    assert loaded_a.settings.selected_product_types == ("spot",)
    with pytest.raises(ValueError, match="spot product type only"):
        CryptoOpportunitySettings(selected_product_types=("perpetual",))


def test_scanner_delivery_flags_and_no_trading_surface():
    settings = make_test_settings()
    assert settings.crypto_opportunity_scanner_enabled is False
    assert settings.crypto_opportunity_delivery_enabled is False
    assert settings.crypto_intel_read_only is True
    import inspect

    from app.crypto import market_snapshots
    from app.crypto.opportunity import basis_funding, scanner

    blob = (
        inspect.getsource(market_snapshots)
        + inspect.getsource(basis_funding)
        + inspect.getsource(scanner)
    ).lower()
    for banned in (
        "api_key",
        "secret_key",
        "create_order",
        "place_order",
        "withdraw",
        "transfer",
        "wallet_balance",
        "private_key",
    ):
        assert banned not in blob
