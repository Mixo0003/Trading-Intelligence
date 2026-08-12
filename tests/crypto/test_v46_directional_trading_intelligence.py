"""Focused offline tests for V4.6 directional trading intelligence foundation."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.crypto.opportunity.directional import (
    DirectionalEvidence,
    DirectionalEvidencePresence,
    DirectionalObservation,
    DirectionalRejectCode,
    DirectionalSide,
    DirectionalStrategyType,
    evaluate_directional_opportunity,
    format_directional_opportunity_card,
    require_volume_volatility_sources,
)
from app.crypto.opportunity.formatting import format_crypto_opportunity_card
from app.crypto.opportunity.models import PaperNetOpportunity
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    CryptoOpportunitySubscription,
)
from app.crypto.product_types import ProductType
from app.crypto.venue_registry import CanonicalInstrument, V43_EXCHANGE_IDS
from app.database import crypto_opportunity_storage as storage
from app.database.storage import initialize_database, upsert_user
from tests.conftest import make_test_settings


def _now() -> datetime:
    return datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)


def _inst(
    *,
    venue_id: str = "binance",
    product_type: ProductType = ProductType.SPOT,
    venue_market_id: str = "BTCUSDT",
    contract_expiry: str | None = None,
) -> CanonicalInstrument:
    return CanonicalInstrument(
        venue_id=venue_id,
        product_type=product_type,
        venue_market_id=venue_market_id,
        base="BTC",
        quote="USDT",
        normalized_symbol="BTC/USDT",
        contract_expiry=contract_expiry,
    )


def _evidence(**overrides) -> DirectionalEvidence:
    payload = {
        "presence": DirectionalEvidencePresence(reference_price=True),
        "best_bid": Decimal("100"),
        "best_ask": Decimal("101"),
        "reference_price": Decimal("100.5"),
        "reference_price_label": "mid",
    }
    payload.update(overrides)
    return DirectionalEvidence(**payload)


def _obs(
    *,
    side: DirectionalSide = DirectionalSide.LONG,
    strategy: DirectionalStrategyType = DirectionalStrategyType.TREND,
    evidence: DirectionalEvidence | None = None,
    instrument: CanonicalInstrument | None = None,
    observed_at: datetime | None = None,
) -> DirectionalObservation:
    return DirectionalObservation(
        instrument=instrument or _inst(),
        side=side,
        strategy_type=strategy,
        evidence=evidence or _evidence(),
        as_of=_now(),
        observed_at=observed_at or _now(),
    )


@pytest.mark.parametrize(
    "strategy",
    list(DirectionalStrategyType),
)
def test_each_directional_strategy_fail_closed_without_formula(strategy):
    evidence = _evidence()
    if strategy is DirectionalStrategyType.ORDER_BOOK_IMBALANCE:
        evidence = _evidence(
            presence=DirectionalEvidencePresence(
                reference_price=True, order_book_depth=True
            ),
            bid_depth_base=Decimal("2"),
            ask_depth_base=Decimal("1"),
        )
    obs = _obs(strategy=strategy, evidence=evidence)
    result, reject = evaluate_directional_opportunity(obs)
    assert result is None
    assert reject.code is DirectionalRejectCode.MISSING_SIGNAL_FORMULA
    assert strategy.value in reject.message or reject.strategy_type == strategy.value


@pytest.mark.parametrize("side", list(DirectionalSide))
def test_long_short_classification_and_card(side):
    obs = _obs(side=side, strategy=DirectionalStrategyType.MOMENTUM)
    _, reject = evaluate_directional_opportunity(obs)
    card = format_directional_opportunity_card(obs, reject)
    assert f"Side: {side.value}" in card
    assert "Strategy / evidence type: momentum" in card
    assert "Symbol: BTC/USDT" in card
    assert "Venue: binance" in card
    assert "mid price: 100.5" in card
    assert "paper-only" in card.lower()
    assert "not guaranteed profit" in card.lower()
    assert "no tp/sl invented" in card.lower()


def test_volume_and_volatility_evidence_fail_closed_without_sources():
    evidence = _evidence(
        presence=DirectionalEvidencePresence(
            reference_price=True, volume=True, volatility=True
        ),
        volume=Decimal("1000"),
        volatility=Decimal("0.02"),
    )
    obs = _obs(strategy=DirectionalStrategyType.BREAKOUT, evidence=evidence)
    source = require_volume_volatility_sources(obs)
    assert source is not None
    assert source.code is DirectionalRejectCode.MISSING_REQUIRED_SOURCE
    result, reject = evaluate_directional_opportunity(obs)
    assert result is None
    assert reject.code is DirectionalRejectCode.MISSING_REQUIRED_SOURCE


def test_order_book_imbalance_requires_depth_and_fail_closed():
    with pytest.raises(ValueError, match="order_book_depth"):
        _obs(strategy=DirectionalStrategyType.ORDER_BOOK_IMBALANCE)
    evidence = _evidence(
        presence=DirectionalEvidencePresence(
            reference_price=True, order_book_depth=True
        ),
        bid_depth_base=Decimal("5"),
        ask_depth_base=Decimal("2"),
    )
    obs = _obs(
        side=DirectionalSide.SHORT,
        strategy=DirectionalStrategyType.ORDER_BOOK_IMBALANCE,
        evidence=evidence,
    )
    _, reject = evaluate_directional_opportunity(obs)
    assert reject.code is DirectionalRejectCode.MISSING_SIGNAL_FORMULA
    card = format_directional_opportunity_card(obs, reject)
    assert "Bid depth (base): 5" in card
    assert "Ask depth (base): 2" in card


def test_stale_missing_contradictory_data_fail_closed():
    with pytest.raises(ValueError, match="observed_at must not be after as_of"):
        _obs(observed_at=datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="best_bid must not exceed best_ask"):
        _evidence(best_bid=Decimal("102"), best_ask=Decimal("101"))
    with pytest.raises(ValueError, match="reference_price capability requires"):
        DirectionalEvidence(
            presence=DirectionalEvidencePresence(reference_price=True),
            reference_price=None,
        )
    with pytest.raises(ValueError, match="must be absent when"):
        DirectionalEvidence(
            presence=DirectionalEvidencePresence(reference_price=False),
            reference_price=Decimal("100"),
        )
    missing_ref = _obs(
        evidence=DirectionalEvidence(
            presence=DirectionalEvidencePresence(reference_price=False)
        )
    )
    result, reject = evaluate_directional_opportunity(missing_ref)
    assert result is None
    assert reject.code is DirectionalRejectCode.MISSING_REQUIRED_EVIDENCE


def test_unsupported_futures_fail_closed():
    fut = _inst(
        product_type=ProductType.PERPETUAL,
        venue_market_id="BTCUSDT-PERP",
    )
    obs = _obs(instrument=fut, strategy=DirectionalStrategyType.TREND)
    result, reject = evaluate_directional_opportunity(obs)
    assert result is None
    assert reject.code is DirectionalRejectCode.UNSUPPORTED_PRODUCT


def test_risk_and_freshness_policies_fail_closed_when_formula_present():
    obs = _obs()
    result, reject = evaluate_directional_opportunity(
        obs, signal_formula=object()
    )
    assert result is None
    assert reject.code is DirectionalRejectCode.MISSING_RISK_POLICY
    result2, reject2 = evaluate_directional_opportunity(
        obs, signal_formula=object(), risk_policy=object()
    )
    assert result2 is None
    assert reject2.code is DirectionalRejectCode.MISSING_FRESHNESS_POLICY
    result3, reject3 = evaluate_directional_opportunity(
        obs,
        signal_formula=object(),
        risk_policy=object(),
        freshness_policy=object(),
    )
    assert result3 is None
    assert reject3.code is DirectionalRejectCode.UNSUPPORTED_STRATEGY


def test_per_user_isolation_unchanged(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "v46.db"))
    initialize_database(settings)
    storage.initialize_crypto_opportunity_schema(settings)
    upsert_user(settings, 11, "a", "A")
    upsert_user(settings, 22, "b", "B")
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(
            telegram_id=11,
            settings=CryptoOpportunitySettings(
                revision=1, selected_exchanges=("binance", "okx")
            ),
        ),
    )
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(
            telegram_id=22,
            settings=CryptoOpportunitySettings(
                revision=1, selected_exchanges=("mexc", "kucoin")
            ),
        ),
    )
    a = storage.get_subscription(settings, 11)
    b = storage.get_subscription(settings, 22)
    assert a is not None and b is not None
    assert a.settings.selected_exchanges == ("binance", "okx")
    assert b.settings.selected_exchanges == ("mexc", "kucoin")
    assert a.settings.selected_product_types == ("spot",)


def test_v44_spot_arb_card_and_settings_unchanged():
    assert "bybit" not in V43_EXCHANGE_IDS
    with pytest.raises(ValueError, match="spot product type only"):
        CryptoOpportunitySettings(selected_product_types=("perpetual",))
    opp = PaperNetOpportunity(
        opportunity_key="k",
        opportunity_uid="u",
        pair="BTC/USDT",
        product_type="spot",
        buy_venue="binance",
        sell_venue="okx",
        buy_ask=101.0,
        sell_bid=102.0,
        buy_vwap=101.0,
        sell_vwap=102.0,
        gross_spread_usd=1.0,
        buy_fee_usd=0.1,
        sell_fee_usd=0.1,
        trading_fees_usd=0.2,
        estimated_slippage=0.01,
        paper_allocation_usd=Decimal("100"),
        executable_base=1.0,
        executable_liquidity_usd=100.0,
        net_profit_usd=0.5,
        roi=0.005,
        signal_age_seconds=1.0,
        checked_at=_now(),
    )
    text = format_crypto_opportunity_card(opp)
    assert "spot cross-exchange paper arbitrage" in text.lower()
    assert "Not LONG/SHORT" in text


def test_scanner_delivery_flags_and_no_trading_apis():
    settings = make_test_settings()
    assert settings.crypto_opportunity_scanner_enabled is False
    assert settings.crypto_opportunity_delivery_enabled is False
    from app.crypto.opportunity import directional, scanner

    blob = (inspect.getsource(directional) + inspect.getsource(scanner)).lower()
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
