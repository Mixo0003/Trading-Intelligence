"""Focused offline tests for V4.8 unified scoring & calibration foundation."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.crypto.opportunity.basis_funding import (
    BasisObservation,
    FoundationRejectCode,
    evaluate_basis_opportunity,
)
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
    NewsEvidenceItem,
    NewsEventClass,
    NewsFundamentalRejectCode,
    NewsOverlayAction,
    NewsProvenance,
    NewsSourceType,
    NewsVerificationStatus,
    OpportunityNewsOverlay,
    evaluate_news_fundamental_overlay,
)
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    CryptoOpportunitySubscription,
)
from app.crypto.opportunity.unified_scoring import (
    OutcomeStatus,
    ScoreComponent,
    ScoreComponentId,
    ScoreRejectCode,
    StrategyEvidenceRef,
    StrategyKind,
    UnifiedScoreRequest,
    build_calibration_record,
    evaluate_unified_score,
    format_unified_score_status_card,
    normalize_directional_evidence,
    normalize_news_evidence,
    normalize_spot_arbitrage_evidence,
    record_opportunity_outcome,
)
from app.crypto.market_snapshots import (
    NormalizedMarketSnapshot,
    SnapshotCapabilityPresence,
    SnapshotProvenance,
)
from app.crypto.product_types import ProductType
from app.crypto.venue_registry import CanonicalInstrument
from app.database import crypto_opportunity_storage as storage
from app.database.storage import initialize_database, upsert_user
from tests.conftest import make_test_settings


def _now() -> datetime:
    return datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


def _inst(**kwargs) -> CanonicalInstrument:
    payload = {
        "venue_id": "binance",
        "product_type": ProductType.SPOT,
        "venue_market_id": "BTCUSDT",
        "base": "BTC",
        "quote": "USDT",
        "normalized_symbol": "BTC/USDT",
    }
    payload.update(kwargs)
    return CanonicalInstrument(**payload)


def _arb() -> PaperNetOpportunity:
    return PaperNetOpportunity(
        opportunity_key="k",
        opportunity_uid="opp-arb-1",
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


def _components(*pairs: tuple[ScoreComponentId, bool, str]) -> tuple[ScoreComponent, ...]:
    return tuple(
        ScoreComponent(component_id=cid, present=present, diagnostic=diag)
        for cid, present, diag in pairs
    )


def _request(**overrides) -> UnifiedScoreRequest:
    ref = normalize_spot_arbitrage_evidence(_arb())
    payload = {
        "opportunity_uid": "opp-arb-1",
        "strategy_kind": StrategyKind.SPOT_ARBITRAGE,
        "components": _components(
            (ScoreComponentId.NET_EDGE, True, "paper net edge present"),
            (ScoreComponentId.FRESHNESS, True, "fresh within policy TBD"),
            (ScoreComponentId.PROVENANCE, True, "public book provenance"),
        ),
        "evidence_refs": (ref,),
        "as_of": _now(),
    }
    payload.update(overrides)
    return UnifiedScoreRequest(**payload)


def test_unified_score_contracts_and_explainable_components():
    request = _request()
    assert request.strategy_kind is StrategyKind.SPOT_ARBITRAGE
    assert {c.component_id for c in request.components} >= {
        ScoreComponentId.NET_EDGE,
        ScoreComponentId.FRESHNESS,
    }
    result, reject = evaluate_unified_score(request)
    assert result is None
    assert reject.code is ScoreRejectCode.MISSING_WEIGHT_POLICY
    card = format_unified_score_status_card(request, reject)
    assert "Explainable components:" in card
    assert "net_edge" in card
    assert "not a probability" in card.lower()
    assert "not guaranteed profit" in card.lower()


def test_strategy_evidence_normalization():
    arb_ref = normalize_spot_arbitrage_evidence(_arb())
    assert arb_ref.strategy_kind is StrategyKind.SPOT_ARBITRAGE
    assert arb_ref.opportunity_uid == "opp-arb-1"

    spot = NormalizedMarketSnapshot(
        instrument=_inst(),
        product_type=ProductType.SPOT,
        presence=SnapshotCapabilityPresence(bid_ask=True),
        provenance=SnapshotProvenance(
            source_id="t", endpoint_path="", observed_at=_now()
        ),
        as_of=_now(),
        best_bid=Decimal("100"),
        best_ask=Decimal("101"),
    )
    perp = NormalizedMarketSnapshot(
        instrument=_inst(
            product_type=ProductType.PERPETUAL, venue_market_id="BTCUSDT-PERP"
        ),
        product_type=ProductType.PERPETUAL,
        presence=SnapshotCapabilityPresence(
            bid_ask=True, mark_price=True, funding=True
        ),
        provenance=SnapshotProvenance(
            source_id="t2", endpoint_path="", observed_at=_now()
        ),
        as_of=_now(),
        best_bid=Decimal("100"),
        best_ask=Decimal("101"),
        mark_price=Decimal("100.5"),
        funding_rate=Decimal("0.0001"),
        funding_time=_now(),
    )
    basis = BasisObservation(
        left=spot,
        right=perp,
        relationship="spot_perpetual",
        as_of=_now(),
    )
    # basis normalize uses observation; evaluate still fail-closed elsewhere
    from app.crypto.opportunity.unified_scoring import normalize_basis_evidence

    basis_ref = normalize_basis_evidence(basis)
    assert basis_ref.strategy_kind is StrategyKind.BASIS

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
    d_ref = normalize_directional_evidence(directional)
    assert d_ref.strategy_kind is StrategyKind.DIRECTIONAL

    news = OpportunityNewsOverlay(
        opportunity_uid="opp-arb-1",
        action=NewsOverlayAction.CONFIRM,
        evidence=NewsEvidenceItem(
            evidence_id="e1",
            event_class=NewsEventClass.CENTRAL_BANK_CPI_FOMC,
            related_asset_label="BTC/USDT",
            related_instrument=_inst(),
            dedupe_key="k1",
            verification_status=NewsVerificationStatus.VERIFIED,
            provenance=NewsProvenance(
                source_id="bls",
                publisher="BLS",
                source_type=NewsSourceType.OFFICIAL,
                url="https://www.bls.gov/news.release/cpi.htm",
                published_at=_now(),
                retrieved_at=_now(),
            ),
            summary="cpi",
        ),
        as_of=_now(),
    )
    n_ref = normalize_news_evidence(news)
    assert n_ref.strategy_kind is StrategyKind.NEWS_FUNDAMENTAL


def test_missing_input_and_policy_fail_closed():
    with pytest.raises(ValueError, match="components must not be empty"):
        UnifiedScoreRequest(
            opportunity_uid="x",
            strategy_kind=StrategyKind.SPOT_ARBITRAGE,
            components=(),
            evidence_refs=(
                StrategyEvidenceRef(
                    strategy_kind=StrategyKind.SPOT_ARBITRAGE,
                    opportunity_uid="x",
                    evidence_summary="s",
                    as_of=_now(),
                ),
            ),
            as_of=_now(),
        )
    empty_refs = _request(evidence_refs=())
    _, reject = evaluate_unified_score(empty_refs)
    assert reject.code is ScoreRejectCode.MISSING_REQUIRED_INPUT
    _, r2 = evaluate_unified_score(_request(), weight_policy=object())
    assert r2.code is ScoreRejectCode.MISSING_THRESHOLD_POLICY
    _, r3 = evaluate_unified_score(
        _request(), weight_policy=object(), threshold_policy=object()
    )
    assert r3.code is ScoreRejectCode.MISSING_RANKING_POLICY
    _, r4 = evaluate_unified_score(
        _request(),
        weight_policy=object(),
        threshold_policy=object(),
        ranking_policy=object(),
    )
    assert r4.code is ScoreRejectCode.SCORE_UNSUPPORTED


def test_stale_and_contradictory_evidence():
    stale = _request(
        components=_components(
            (ScoreComponentId.FRESHNESS, False, "stale book evidence"),
            (ScoreComponentId.NET_EDGE, True, "edge"),
        )
    )
    _, reject = evaluate_unified_score(stale)
    assert reject.code is ScoreRejectCode.STALE_EVIDENCE
    contradictory = _request(
        components=_components(
            (ScoreComponentId.PROVENANCE, True, "provenance ok"),
            (
                ScoreComponentId.EVIDENCE_COMPLETENESS,
                False,
                "contradictory missing fields",
            ),
        )
    )
    _, reject2 = evaluate_unified_score(contradictory)
    assert reject2.code is ScoreRejectCode.CONTRADICTORY_EVIDENCE


def test_outcome_lifecycle_immutability_and_calibration():
    record, err = record_opportunity_outcome(
        opportunity_uid="opp-arb-1",
        status=OutcomeStatus.SUCCEEDED,
        recorded_at=_now(),
        observed_at=_now(),
        note="paper outcome",
    )
    assert err is None and record is not None
    assert record.immutable is True
    again, err2 = record_opportunity_outcome(
        opportunity_uid="opp-arb-1",
        status=OutcomeStatus.FAILED,
        recorded_at=_now(),
        existing=record,
    )
    assert again is None
    assert err2 is not None
    assert "immutable" in err2.message
    for status in OutcomeStatus:
        rec, e = record_opportunity_outcome(
            opportunity_uid=f"opp-{status.value}",
            status=status,
            recorded_at=_now(),
        )
        assert e is None and rec is not None
        assert rec.status is status
    cal, cerr = build_calibration_record(
        calibration_id="cal-1", outcome=record, as_of=_now()
    )
    assert cerr is None and cal is not None
    assert cal.opportunity_uid == record.opportunity_uid


def test_per_user_isolation(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "v48.db"))
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


def test_unchanged_previous_version_behavior():
    text = format_crypto_opportunity_card(_arb())
    assert "spot cross-exchange paper arbitrage" in text.lower()
    assert "Not LONG/SHORT" in text

    directional = DirectionalObservation(
        instrument=_inst(),
        side=DirectionalSide.SHORT,
        strategy_type=DirectionalStrategyType.MOMENTUM,
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

    news = OpportunityNewsOverlay(
        opportunity_uid="opp-arb-1",
        action=NewsOverlayAction.SUPPRESS,
        evidence=NewsEvidenceItem(
            evidence_id="e2",
            event_class=NewsEventClass.REGULATORY,
            related_asset_label="BTC/USDT",
            related_instrument=_inst(),
            dedupe_key="k2",
            verification_status=NewsVerificationStatus.VERIFIED,
            provenance=NewsProvenance(
                source_id="sec",
                publisher="SEC",
                source_type=NewsSourceType.REGULATORY,
                url="https://www.sec.gov/",
                published_at=_now(),
                retrieved_at=_now(),
            ),
            summary="regulatory",
        ),
        as_of=_now(),
    )
    _, n_reject = evaluate_news_fundamental_overlay(news)
    assert n_reject.code is NewsFundamentalRejectCode.MISSING_SOURCE_REGISTRY

    spot = NormalizedMarketSnapshot(
        instrument=_inst(),
        product_type=ProductType.SPOT,
        presence=SnapshotCapabilityPresence(bid_ask=True),
        provenance=SnapshotProvenance(
            source_id="t", endpoint_path="", observed_at=_now()
        ),
        as_of=_now(),
        best_bid=Decimal("100"),
        best_ask=Decimal("101"),
    )
    perp = NormalizedMarketSnapshot(
        instrument=_inst(
            venue_id="okx",
            product_type=ProductType.PERPETUAL,
            venue_market_id="BTC-USDT-SWAP",
        ),
        product_type=ProductType.PERPETUAL,
        presence=SnapshotCapabilityPresence(
            bid_ask=True, mark_price=True, funding=True
        ),
        provenance=SnapshotProvenance(
            source_id="t2", endpoint_path="", observed_at=_now()
        ),
        as_of=_now(),
        best_bid=Decimal("100"),
        best_ask=Decimal("101"),
        mark_price=Decimal("100.5"),
        funding_rate=Decimal("0.0001"),
        funding_time=_now(),
    )
    basis = BasisObservation(
        left=spot, right=perp, relationship="spot_perpetual", as_of=_now()
    )
    _, b_reject = evaluate_basis_opportunity(basis)
    assert b_reject.code is FoundationRejectCode.MISSING_FEE_POLICY


def test_scanner_delivery_safety_and_no_trading_apis():
    settings = make_test_settings()
    assert settings.crypto_opportunity_scanner_enabled is False
    assert settings.crypto_opportunity_delivery_enabled is False
    from app.crypto.opportunity import scanner, unified_scoring

    blob = (
        inspect.getsource(unified_scoring) + inspect.getsource(scanner)
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
