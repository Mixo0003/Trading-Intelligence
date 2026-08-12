"""Focused offline tests for V4.7 news & fundamental intelligence foundation."""

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
)
from app.crypto.opportunity.formatting import format_crypto_opportunity_card
from app.crypto.opportunity.models import PaperNetOpportunity
from app.crypto.opportunity.news_fundamentals import (
    AssetRuleContext,
    NewsEvidenceItem,
    NewsEventClass,
    NewsFundamentalRejectCode,
    NewsOverlayAction,
    NewsProvenance,
    NewsSourceType,
    NewsVerificationStatus,
    OpportunityNewsOverlay,
    create_signal_from_news_alone,
    dedupe_news_evidence,
    evaluate_news_fundamental_overlay,
    format_news_fundamental_status_card,
    reject_contradictory_overlays,
    run_background_news_integration,
    validate_news_evidence_item,
)
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
    return datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)


def _inst(
    *,
    venue_id: str = "binance",
    product_type: ProductType = ProductType.SPOT,
    venue_market_id: str = "BTCUSDT",
) -> CanonicalInstrument:
    return CanonicalInstrument(
        venue_id=venue_id,
        product_type=product_type,
        venue_market_id=venue_market_id,
        base="BTC",
        quote="USDT",
        normalized_symbol="BTC/USDT",
    )


def _prov(**overrides) -> NewsProvenance:
    payload = {
        "source_id": "bls_cpi",
        "publisher": "U.S. Bureau of Labor Statistics",
        "source_type": NewsSourceType.OFFICIAL,
        "url": "https://www.bls.gov/news.release/cpi.htm",
        "published_at": _now(),
        "retrieved_at": _now(),
    }
    payload.update(overrides)
    return NewsProvenance(**payload)


def _evidence(**overrides) -> NewsEvidenceItem:
    payload = {
        "evidence_id": "ev-cpi-1",
        "event_class": NewsEventClass.CENTRAL_BANK_CPI_FOMC,
        "related_asset_label": "BTC/USDT",
        "related_instrument": _inst(),
        "dedupe_key": "bls:cpi:2026-08",
        "verification_status": NewsVerificationStatus.VERIFIED,
        "provenance": _prov(),
        "summary": "Official CPI release referenced for overlay only",
    }
    payload.update(overrides)
    return NewsEvidenceItem(**payload)


def _overlay(
    *,
    action: NewsOverlayAction = NewsOverlayAction.CONFIRM,
    evidence: NewsEvidenceItem | None = None,
    opportunity_uid: str = "crypto_v1:BTC/USDT:binance:okx:spot:buy_low_sell_high",
) -> OpportunityNewsOverlay:
    return OpportunityNewsOverlay(
        opportunity_uid=opportunity_uid,
        action=action,
        evidence=evidence or _evidence(),
        as_of=_now(),
    )


def test_evidence_normalization_and_provenance():
    item = _evidence()
    assert item.provenance.url.startswith("https://")
    assert item.provenance.source_type is NewsSourceType.OFFICIAL
    assert item.provenance.published_at == item.provenance.retrieved_at
    with pytest.raises(ValueError, match="http"):
        _prov(url="ftp://example.com/x")
    with pytest.raises(ValueError, match="retrieved_at"):
        _prov(
            published_at=_now(),
            retrieved_at=datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("event_class", list(NewsEventClass))
def test_official_event_classes_and_source_validation(event_class):
    item = _evidence(event_class=event_class, evidence_id=f"ev-{event_class.value}")
    assert validate_news_evidence_item(item) is None
    unverified = _evidence(verification_status=NewsVerificationStatus.UNVERIFIED)
    reject = validate_news_evidence_item(unverified)
    assert reject is not None
    assert reject.code is NewsFundamentalRejectCode.UNVERIFIED_EVIDENCE


def test_deduplication_and_contradictory_overlays():
    a = _evidence(evidence_id="a", dedupe_key="same")
    b = _evidence(evidence_id="b", dedupe_key="same")
    kept, rejects = dedupe_news_evidence((a, b))
    assert len(kept) == 1
    assert rejects[0].code is NewsFundamentalRejectCode.DUPLICATE_EVIDENCE
    contradictory = reject_contradictory_overlays(
        (
            _overlay(action=NewsOverlayAction.CONFIRM, evidence=a),
            _overlay(action=NewsOverlayAction.REJECT, evidence=b),
        )
    )
    assert contradictory is not None
    assert contradictory.code is NewsFundamentalRejectCode.CONTRADICTORY_EVIDENCE


def test_stale_irrelevant_and_ambiguous_asset_rejection():
    with pytest.raises(ValueError, match="ambiguous asset label"):
        _evidence(related_asset_label="Gold")
    with pytest.raises(ValueError, match="ambiguous asset label"):
        _evidence(related_asset_label="Oil")
    irrelevant = _evidence(
        related_instrument=None,
        related_asset_label="UNKNOWN-ASSET",
        asset_rules=None,
        event_class=NewsEventClass.GEOPOLITICS,
    )
    reject = validate_news_evidence_item(irrelevant)
    assert reject is not None
    assert reject.code is NewsFundamentalRejectCode.IRRELEVANT_EVIDENCE


def test_event_to_asset_mapping_and_venue_product_rules():
    bybit = _evidence(related_instrument=_inst(venue_id="bybit"))
    reject = validate_news_evidence_item(bybit)
    assert reject is not None
    assert reject.code is NewsFundamentalRejectCode.UNSUPPORTED_VENUE
    fut = _evidence(
        related_instrument=_inst(
            product_type=ProductType.PERPETUAL, venue_market_id="BTCUSDT-PERP"
        )
    )
    reject_f = validate_news_evidence_item(fut)
    assert reject_f is not None
    assert reject_f.code is NewsFundamentalRejectCode.UNSUPPORTED_PRODUCT


def test_news_confirm_reject_downgrade_suppress_existing_opportunity():
    for action in NewsOverlayAction:
        overlay = _overlay(action=action)
        result, reject = evaluate_news_fundamental_overlay(overlay)
        assert result is None
        assert reject.code is NewsFundamentalRejectCode.MISSING_SOURCE_REGISTRY
        assert overlay.opportunity_uid
        card = format_news_fundamental_status_card(overlay, reject)
        assert action.value in card
        assert "Existing opportunity:" in card
        assert "news alone never creates" in card.lower()


def test_news_alone_never_creates_signal():
    result, reject = create_signal_from_news_alone(_evidence())
    assert result is None
    assert reject.code is NewsFundamentalRejectCode.NEWS_ALONE_SIGNAL_FORBIDDEN
    bg, bg_reject = run_background_news_integration(
        existing_opportunity_uids=(),
        evidence_batch=(_evidence(),),
    )
    assert bg is None
    assert bg_reject.code is NewsFundamentalRejectCode.MISSING_OPPORTUNITY_REF


def test_asset_rule_validation_for_trading_rules_event():
    rules = AssetRuleContext(
        venue_id="okx",
        product_type=ProductType.SPOT,
        settlement="USDT",
        trading_hours="24/7",
        fee_note="spot schedule required",
        restrictions="read-only paper",
    )
    item = _evidence(
        event_class=NewsEventClass.ASSET_VENUE_TRADING_RULES,
        related_instrument=None,
        related_asset_label="BTC/USDT-spot-rules",
        asset_rules=rules,
    )
    assert validate_news_evidence_item(item) is None
    missing_rules = _evidence(
        event_class=NewsEventClass.ASSET_VENUE_TRADING_RULES,
        related_instrument=_inst(venue_id="okx"),
        asset_rules=None,
    )
    result, reject = evaluate_news_fundamental_overlay(
        _overlay(evidence=missing_rules),
        source_registry=object(),
        freshness_policy=object(),
    )
    assert result is None
    assert reject.code is NewsFundamentalRejectCode.MISSING_ASSET_RULES


def test_policies_fail_closed_and_background_integration():
    overlay = _overlay()
    _, r1 = evaluate_news_fundamental_overlay(overlay, source_registry=object())
    assert r1.code is NewsFundamentalRejectCode.MISSING_FRESHNESS_POLICY
    _, r2 = evaluate_news_fundamental_overlay(
        overlay,
        source_registry=object(),
        freshness_policy=object(),
        require_ranking=True,
    )
    assert r2.code is NewsFundamentalRejectCode.MISSING_RANKING_POLICY
    _, r3 = evaluate_news_fundamental_overlay(
        overlay, source_registry=object(), freshness_policy=object()
    )
    assert r3.code is NewsFundamentalRejectCode.UNSUPPORTED_OVERLAY
    _, bg = run_background_news_integration(
        existing_opportunity_uids=("opp-1",),
        evidence_batch=(_evidence(),),
    )
    assert bg.code is NewsFundamentalRejectCode.MISSING_SOURCE_REGISTRY


def test_per_user_isolation(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "v47.db"))
    initialize_database(settings)
    storage.initialize_crypto_opportunity_schema(settings)
    upsert_user(settings, 1, "a", "A")
    upsert_user(settings, 2, "b", "B")
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(
            telegram_id=1,
            settings=CryptoOpportunitySettings(
                revision=1, selected_exchanges=("binance", "okx")
            ),
        ),
    )
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(
            telegram_id=2,
            settings=CryptoOpportunitySettings(
                revision=1, selected_exchanges=("mexc", "kucoin")
            ),
        ),
    )
    a = storage.get_subscription(settings, 1)
    b = storage.get_subscription(settings, 2)
    assert a is not None and b is not None
    assert a.settings.selected_exchanges != b.settings.selected_exchanges


def test_unchanged_v44_spot_arb_and_v46_directional():
    assert "bybit" not in V43_EXCHANGE_IDS
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
    arb = format_crypto_opportunity_card(opp)
    assert "spot cross-exchange paper arbitrage" in arb.lower()
    assert "Not LONG/SHORT" in arb
    assert "news" in arb.lower()

    directional = DirectionalObservation(
        instrument=_inst(),
        side=DirectionalSide.LONG,
        strategy_type=DirectionalStrategyType.TREND,
        evidence=DirectionalEvidence(
            presence=DirectionalEvidencePresence(reference_price=True),
            reference_price=Decimal("100"),
            reference_price_label="mid",
        ),
        as_of=_now(),
        observed_at=_now(),
    )
    _, d_reject = evaluate_directional_opportunity(directional)
    assert d_reject.code is DirectionalRejectCode.MISSING_SIGNAL_FORMULA


def test_scanner_delivery_safety_and_no_trading_apis():
    settings = make_test_settings()
    assert settings.crypto_opportunity_scanner_enabled is False
    assert settings.crypto_opportunity_delivery_enabled is False
    from app.crypto.opportunity import news_fundamentals, scanner

    blob = (
        inspect.getsource(news_fundamentals) + inspect.getsource(scanner)
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
