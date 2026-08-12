"""Focused offline tests for V4.10 scoring policy & ranking enablement."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
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
from app.crypto.opportunity.scoring_policy import (
    ConfidenceBand,
    ConfidencePolicy,
    PolicyRejectCode,
    RankingPolicy,
    RankingSortKey,
    ScoringPolicy,
    ThresholdPolicy,
    WeightPolicy,
    default_scoring_policy,
    format_scored_opportunity_card,
    rank_scored_opportunities,
    score_with_policy,
    validate_scoring_policy,
)
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    CryptoOpportunitySubscription,
)
from app.crypto.opportunity.source_registry import default_authoritative_registry
from app.crypto.opportunity.unified_scoring import (
    OutcomeStatus,
    ScoreComponent,
    ScoreComponentId,
    ScoreRejectCode,
    StrategyKind,
    UnifiedScoreRequest,
    evaluate_unified_score,
    format_unified_score_status_card,
    normalize_spot_arbitrage_evidence,
    record_opportunity_outcome,
)
from app.crypto.opportunity.verified_news_ingest import refuse_news_alone_signal
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
    return datetime(2026, 8, 12, 19, 0, tzinfo=timezone.utc)


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


def _arb(uid: str = "opp-arb-1") -> PaperNetOpportunity:
    return PaperNetOpportunity(
        opportunity_key="k",
        opportunity_uid=uid,
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


def _components(
    *pairs: tuple[ScoreComponentId, bool, str],
) -> tuple[ScoreComponent, ...]:
    return tuple(
        ScoreComponent(component_id=cid, present=present, diagnostic=diag)
        for cid, present, diag in pairs
    )


def _request(uid: str = "opp-arb-1", **overrides) -> UnifiedScoreRequest:
    ref = normalize_spot_arbitrage_evidence(_arb(uid))
    payload = {
        "opportunity_uid": uid,
        "strategy_kind": StrategyKind.SPOT_ARBITRAGE,
        "components": _components(
            (ScoreComponentId.NET_EDGE, True, "paper net edge present"),
            (ScoreComponentId.LIQUIDITY, True, "executable liquidity present"),
            (ScoreComponentId.FRESHNESS, True, "fresh within caller policy"),
            (ScoreComponentId.PROVENANCE, True, "public book provenance"),
        ),
        "evidence_refs": (ref,),
        "as_of": _now(),
    }
    payload.update(overrides)
    return UnifiedScoreRequest(**payload)


def _approved_policy(
    *,
    version: str = "score-v4.10-test-1",
    inclusion_min: Decimal = Decimal("0.50"),
    approved: bool = True,
    weights_approved: bool = True,
    ranking_keys: tuple[RankingSortKey, ...] = (
        RankingSortKey.SCORE_DESC,
        RankingSortKey.OPPORTUNITY_UID_ASC,
    ),
) -> ScoringPolicy:
    """Explicit test-only policy — not a product default."""
    return ScoringPolicy(
        score_version=version,
        provenance_note="test fixture policy for V4.10 unit tests only",
        approved=approved,
        weights=WeightPolicy(
            score_version=version,
            provenance_note="test weight provenance",
            approved=weights_approved,
            component_weights=(
                (ScoreComponentId.NET_EDGE, Decimal("0.40")),
                (ScoreComponentId.LIQUIDITY, Decimal("0.25")),
                (ScoreComponentId.FRESHNESS, Decimal("0.20")),
                (ScoreComponentId.PROVENANCE, Decimal("0.15")),
            ),
            required_components=(
                ScoreComponentId.NET_EDGE,
                ScoreComponentId.PROVENANCE,
            ),
        ),
        thresholds=ThresholdPolicy(
            score_version=version,
            provenance_note="test threshold provenance",
            approved=True,
            inclusion_min=inclusion_min,
        ),
        ranking=RankingPolicy(
            score_version=version,
            provenance_note="test ranking provenance",
            approved=True,
            sort_keys=ranking_keys,
        ),
        confidence=ConfidencePolicy(
            score_version=version,
            provenance_note="test confidence provenance",
            approved=True,
            bands=(
                ConfidenceBand(
                    min_inclusive=Decimal("0"),
                    max_exclusive=Decimal("0.50"),
                    label="low",
                ),
                ConfidenceBand(
                    min_inclusive=Decimal("0.50"),
                    max_exclusive=Decimal("0.80"),
                    label="medium",
                ),
                ConfidenceBand(
                    min_inclusive=Decimal("0.80"),
                    max_exclusive=Decimal("1"),
                    label="high",
                ),
            ),
        ),
    )


def test_policy_validation_complete_approved():
    policy = _approved_policy()
    assert validate_scoring_policy(policy) is None
    assert default_scoring_policy() is None


def test_missing_and_unapproved_policy_fail_closed():
    missing = validate_scoring_policy(None)
    assert missing is not None
    assert missing.code is PolicyRejectCode.MISSING_POLICY

    unapproved = validate_scoring_policy(_approved_policy(approved=False))
    assert unapproved is not None
    assert unapproved.code is PolicyRejectCode.POLICY_NOT_APPROVED

    partial = validate_scoring_policy(
        _approved_policy(weights_approved=False)
    )
    assert partial is not None
    assert partial.code is PolicyRejectCode.POLICY_NOT_APPROVED


def test_contradictory_score_versions_fail_closed():
    base = _approved_policy(version="v-a")
    bad = ScoringPolicy(
        score_version="v-a",
        provenance_note=base.provenance_note,
        approved=True,
        weights=base.weights,
        thresholds=ThresholdPolicy(
            score_version="v-b",
            provenance_note="mismatched",
            approved=True,
            inclusion_min=Decimal("0.1"),
        ),
        ranking=base.ranking,
        confidence=base.confidence,
    )
    err = validate_scoring_policy(bad)
    assert err is not None
    assert err.code is PolicyRejectCode.CONTRADICTORY_POLICY


def test_incomplete_weight_construction_rejected():
    with pytest.raises(ValueError, match="sum to exactly 1"):
        WeightPolicy(
            score_version="x",
            provenance_note="p",
            approved=True,
            component_weights=((ScoreComponentId.NET_EDGE, Decimal("0.5")),),
            required_components=(ScoreComponentId.NET_EDGE,),
        )
    with pytest.raises(ValueError, match="opportunity_uid"):
        RankingPolicy(
            score_version="x",
            provenance_note="p",
            approved=True,
            sort_keys=(RankingSortKey.SCORE_DESC,),
        )


def test_score_with_policy_explainable_components_and_version():
    policy = _approved_policy()
    scored, reject = score_with_policy(_request(), policy)
    assert reject is None and scored is not None
    assert scored.score_version == policy.score_version
    assert scored.score == Decimal("1.00")
    assert scored.confidence_label == "high"
    assert {c.component_id for c in scored.contributions} >= {
        ScoreComponentId.NET_EDGE,
        ScoreComponentId.LIQUIDITY,
    }
    card = format_scored_opportunity_card(scored)
    assert "Explainable contributions:" in card
    assert "not a probability" in card.lower()
    assert "not guaranteed profit" in card.lower()
    assert policy.score_version in card


def test_score_below_threshold_and_missing_required():
    policy = _approved_policy(inclusion_min=Decimal("0.95"))
    partial = _request(
        components=_components(
            (ScoreComponentId.NET_EDGE, True, "edge"),
            (ScoreComponentId.LIQUIDITY, False, "thin book"),
            (ScoreComponentId.FRESHNESS, False, "aging"),
            (ScoreComponentId.PROVENANCE, True, "ok"),
        )
    )
    scored, reject = score_with_policy(partial, policy)
    assert scored is None
    assert reject is not None
    assert "below inclusion_min" in reject.message

    missing_req = _request(
        components=_components(
            (ScoreComponentId.LIQUIDITY, True, "liq"),
            (ScoreComponentId.FRESHNESS, True, "fresh"),
        )
    )
    scored2, reject2 = score_with_policy(missing_req, policy)
    assert scored2 is None
    assert reject2 is not None
    assert reject2.code is ScoreRejectCode.MISSING_REQUIRED_INPUT


def test_deterministic_ranking_and_mixed_version_reject():
    policy = _approved_policy()
    high = _request(uid="opp-b")
    mid = _request(
        uid="opp-a",
        components=_components(
            (ScoreComponentId.NET_EDGE, True, "edge"),
            (ScoreComponentId.LIQUIDITY, True, "liq"),
            (ScoreComponentId.FRESHNESS, False, "aging"),
            (ScoreComponentId.PROVENANCE, True, "ok"),
        ),
        as_of=_now() - timedelta(minutes=1),
    )
    s_high, e1 = score_with_policy(high, policy)
    s_mid, e2 = score_with_policy(mid, policy)
    assert e1 is None and e2 is None
    assert s_high is not None and s_mid is not None
    assert s_high.score > s_mid.score

    ranked, err = rank_scored_opportunities((s_mid, s_high), policy)
    assert err is None and ranked is not None
    assert [item.opportunity_uid for item in ranked] == ["opp-b", "opp-a"]

    # Tie on score → UID ascending
    twin = _request(uid="opp-c")
    s_twin, e3 = score_with_policy(twin, policy)
    assert e3 is None and s_twin is not None
    tied, err2 = rank_scored_opportunities((s_twin, s_high), policy)
    assert err2 is None and tied is not None
    assert [item.opportunity_uid for item in tied] == ["opp-b", "opp-c"]

    other = _approved_policy(version="score-other")
    s_other, e4 = score_with_policy(_request(uid="opp-z"), other)
    assert e4 is None and s_other is not None
    mixed, err3 = rank_scored_opportunities((s_high, s_other), policy)
    assert mixed is None
    assert err3 is not None
    assert err3.code is PolicyRejectCode.MIXED_SCORE_VERSION


def test_outcome_immutability_preserved():
    record, err = record_opportunity_outcome(
        opportunity_uid="opp-arb-1",
        status=OutcomeStatus.SUCCEEDED,
        recorded_at=_now(),
    )
    assert err is None and record is not None
    again, err2 = record_opportunity_outcome(
        opportunity_uid="opp-arb-1",
        status=OutcomeStatus.FAILED,
        recorded_at=_now(),
        existing=record,
    )
    assert again is None and err2 is not None
    assert "immutable" in err2.message


def test_v48_foundation_still_fail_closed_without_typed_policy():
    result, reject = evaluate_unified_score(_request())
    assert result is None
    assert reject.code is ScoreRejectCode.MISSING_WEIGHT_POLICY
    _, r4 = evaluate_unified_score(
        _request(),
        weight_policy=object(),
        threshold_policy=object(),
        ranking_policy=object(),
    )
    assert r4.code is ScoreRejectCode.SCORE_UNSUPPORTED
    card = format_unified_score_status_card(_request(), reject)
    assert "Explainable components:" in card


def test_per_user_filters_history_isolation(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "v410.db"))
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
    assert a.telegram_id != b.telegram_id


def test_v44_through_v49_regressions_unchanged():
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

    sources = default_authoritative_registry().iter_sources()
    assert sources
    alone_result, alone = refuse_news_alone_signal(news.evidence)
    assert alone_result is None
    assert alone is not None
    assert alone.code is NewsFundamentalRejectCode.NEWS_ALONE_SIGNAL_FORBIDDEN


def test_scanner_delivery_safety_and_no_trading_apis():
    settings = make_test_settings()
    assert settings.crypto_opportunity_scanner_enabled is False
    assert settings.crypto_opportunity_delivery_enabled is False
    from app.crypto.opportunity import scanner, scoring_policy, unified_scoring

    blob = (
        inspect.getsource(scoring_policy)
        + inspect.getsource(unified_scoring)
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
