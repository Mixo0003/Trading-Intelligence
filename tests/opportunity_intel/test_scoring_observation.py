"""V2.2.3 Checkpoint 3 — canonical score and observation builder."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.copy_intel.api_result import ApiSuccess
from app.copy_intel.order_book import parse_validated_order_book
from app.opportunity_intel.discovery import OutcomeCandidate, ResultStatus
from app.opportunity_intel.history import FreshnessStatus, HistoryDirection, MovementResult
from app.opportunity_intel.models import (
    ObservationMode,
    OpportunityDiagnosticCode,
    OpportunityReasonCode,
)
from app.opportunity_intel.observation import (
    ObservationAccepted,
    ObservationRejected,
    build_canonical_observation,
    thresholds_for_mode,
)
from app.opportunity_intel.scoring import (
    SCORE_VERSION,
    compute_canonical_score,
    compute_score_components,
    round_score,
)
from app.opportunity_intel.settings import ALLOCATION_PRESETS_USD, BALANCED_PAPER_ALLOCATION_USD

ASSET = "token-score-1"
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)


def _book(
    *,
    timestamp: float = 990.0,
    bid: str = "0.48",
    ask: str = "0.52",
    ask_size: str = "5000",
    bid_size: str = "200",
):
    payload = {
        "asset_id": ASSET,
        "timestamp": timestamp,
        "hash": "hash-score",
        "bids": [{"price": bid, "size": bid_size}],
        "asks": [{"price": ask, "size": ask_size}],
    }
    result = parse_validated_order_book(
        payload, requested_asset=ASSET, received_at=1000.0
    )
    assert isinstance(result, ApiSuccess)
    return result.data


def _candidate(
    *,
    liquidity: str = "50000",
    volume: str = "100000",
    mode: ObservationMode = ObservationMode.NORMAL,
) -> OutcomeCandidate:
    return OutcomeCandidate(
        event_id="event-1",
        market_id="market-1",
        condition_id="condition-1",
        event_slug="slug-1",
        question="Will it happen?",
        category_ids=("politics",),
        mode=mode,
        liquidity_usd=Decimal(liquidity),
        volume_24h_usd=Decimal(volume),
        metadata_at=NOW - timedelta(minutes=2),
        end_at=NOW + timedelta(days=2),
        asset_id=ASSET,
        outcome="Yes",
        outcome_index=0,
        price_hint=Decimal("0.50"),
    )


def _movement(
    *,
    change_5m: str = "4",
    change_15m: str = "6",
    change_1h: str = "8",
    latest: str = "0.50",
) -> MovementResult:
    return MovementResult(
        asset_id=ASSET,
        status=ResultStatus.COMPLETE,
        freshness=FreshnessStatus.FRESH,
        diagnostic_code=None,
        latest_price=Decimal(latest),
        latest_at=NOW - timedelta(seconds=10),
        price_5m=Decimal(latest) - Decimal(change_5m) / Decimal("100"),
        price_15m=Decimal(latest) - Decimal(change_15m) / Decimal("100"),
        price_1h=Decimal(latest) - Decimal(change_1h) / Decimal("100"),
        change_5m_pp=Decimal(change_5m),
        change_15m_pp=Decimal(change_15m),
        change_1h_pp=Decimal(change_1h),
        absolute_change_5m_pp=abs(Decimal(change_5m)),
        absolute_change_15m_pp=abs(Decimal(change_15m)),
        absolute_change_1h_pp=abs(Decimal(change_1h)),
        direction=HistoryDirection.UP,
    )


def test_component_calculations_and_rounding():
    components = compute_score_components(
        absolute_change_5m_pp=Decimal("3"),
        absolute_change_15m_pp=Decimal("5"),
        absolute_change_1h_pp=Decimal("10"),
        volume_24h_usd=Decimal("25000"),
        liquidity_usd=Decimal("10000"),
        visible_ask_depth_usd=Decimal("500"),
        canonical_slippage=Decimal("0"),
        relative_spread=Decimal("0"),
    )
    assert components.movement_5m == Decimal("10")
    assert components.movement_15m == Decimal("15")
    assert components.movement_1h == Decimal("20")
    assert components.volume == Decimal("0")
    assert components.liquidity == Decimal("0")
    assert components.depth == Decimal("10")
    assert components.slippage == Decimal("10")
    assert components.tightness == Decimal("5")
    assert round_score(Decimal("76.75")) == Decimal("76.8")
    assert round_score(Decimal("76.74")) == Decimal("76.7")
    assert round_score(Decimal("150")) == Decimal("100.0")


def test_logarithmic_volume_liquidity_boundaries():
    low = compute_score_components(
        absolute_change_5m_pp=Decimal("0"),
        absolute_change_15m_pp=Decimal("0"),
        absolute_change_1h_pp=Decimal("0"),
        volume_24h_usd=Decimal("25000"),
        liquidity_usd=Decimal("10000"),
        visible_ask_depth_usd=Decimal("0"),
        canonical_slippage=Decimal("0.02"),
        relative_spread=Decimal("0.10"),
    )
    high = compute_score_components(
        absolute_change_5m_pp=Decimal("0"),
        absolute_change_15m_pp=Decimal("0"),
        absolute_change_1h_pp=Decimal("0"),
        volume_24h_usd=Decimal("1000000"),
        liquidity_usd=Decimal("500000"),
        visible_ask_depth_usd=Decimal("0"),
        canonical_slippage=Decimal("0.02"),
        relative_spread=Decimal("0.10"),
    )
    assert low.volume == Decimal("0")
    assert low.liquidity == Decimal("0")
    assert high.volume == Decimal("15")
    assert high.liquidity == Decimal("15")
    assert low.slippage == Decimal("0")
    assert low.tightness == Decimal("0")


def test_canonical_allocation_fixed_at_100():
    with pytest.raises(ValueError, match="canonical allocation"):
        compute_score_components(
            absolute_change_5m_pp=Decimal("0"),
            absolute_change_15m_pp=Decimal("0"),
            absolute_change_1h_pp=Decimal("0"),
            volume_24h_usd=Decimal("25000"),
            liquidity_usd=Decimal("10000"),
            visible_ask_depth_usd=Decimal("0"),
            canonical_slippage=Decimal("0"),
            relative_spread=Decimal("0"),
            canonical_allocation_usd=Decimal("250"),
        )
    assert BALANCED_PAPER_ALLOCATION_USD in ALLOCATION_PRESETS_USD
    assert BALANCED_PAPER_ALLOCATION_USD == Decimal("100")


def test_eligible_observation_deterministic_and_identical_inputs():
    book = _book(bid="0.495", ask="0.505", ask_size="5000")
    candidate = _candidate()
    movement = _movement(latest="0.50")
    first = build_canonical_observation(
        candidate=candidate,
        movement=movement,
        book=book,
        mode=ObservationMode.NORMAL,
    )
    second = build_canonical_observation(
        candidate=candidate,
        movement=movement,
        book=book,
        mode=ObservationMode.NORMAL,
    )
    assert isinstance(first, ObservationAccepted)
    assert isinstance(second, ObservationAccepted)
    assert first.score == second.score
    assert first.score_version == SCORE_VERSION == second.score_version
    assert first.reason_codes == second.reason_codes
    assert first.observation.canonical_execution.allocation_usd == Decimal("100")
    assert first.observation.midpoint == Decimal(str(book.midpoint))
    assert OpportunityReasonCode.FAST_5M_MOVE in first.reason_codes
    assert len(first.allocation_metrics) == len(ALLOCATION_PRESETS_USD)


def test_movement_trigger_missing_rejects():
    result = build_canonical_observation(
        candidate=_candidate(),
        movement=_movement(change_5m="0.5", change_15m="1", change_1h="1"),
        book=_book(),
        mode=ObservationMode.NORMAL,
    )
    assert isinstance(result, ObservationRejected)
    assert OpportunityDiagnosticCode.MOVEMENT_TRIGGER_MISSING in result.diagnostic_codes


def test_history_book_disagreement_rejects():
    result = build_canonical_observation(
        candidate=_candidate(),
        movement=_movement(latest="0.80"),
        book=_book(),
        mode=ObservationMode.NORMAL,
    )
    assert isinstance(result, ObservationRejected)
    assert OpportunityDiagnosticCode.HISTORY_BOOK_MISMATCH in result.diagnostic_codes


def test_midpoint_bounds_and_spread_filters():
    # midpoint ~0.03
    low = build_canonical_observation(
        candidate=_candidate(),
        movement=_movement(latest="0.03"),
        book=_book(bid="0.02", ask="0.04", ask_size="10000"),
        mode=ObservationMode.NORMAL,
    )
    assert isinstance(low, ObservationRejected)
    assert OpportunityDiagnosticCode.MIDPOINT_OUT_OF_BOUNDS in low.diagnostic_codes

    wide = build_canonical_observation(
        candidate=_candidate(),
        movement=_movement(latest="0.50"),
        book=_book(bid="0.40", ask="0.60", ask_size="5000"),
        mode=ObservationMode.NORMAL,
    )
    assert isinstance(wide, ObservationRejected)
    assert OpportunityDiagnosticCode.SPREAD_TOO_WIDE in wide.diagnostic_codes


def test_score_threshold_and_insufficient_depth():
    shallow = build_canonical_observation(
        candidate=_candidate(),
        movement=_movement(latest="0.50"),
        book=_book(ask_size="50"),  # 26 USD ask depth
        mode=ObservationMode.NORMAL,
    )
    assert isinstance(shallow, ObservationRejected)
    assert OpportunityDiagnosticCode.INSUFFICIENT_DEPTH in shallow.diagnostic_codes


def test_strict_live_thresholds_stronger_than_normal():
    normal = thresholds_for_mode(ObservationMode.NORMAL)
    strict = thresholds_for_mode(ObservationMode.LIVE_STRICT)
    assert strict.minimum_score > normal.minimum_score
    assert strict.minimum_liquidity_usd > normal.minimum_liquidity_usd
    assert strict.move_5m_pp > normal.move_5m_pp

    # Passes normal liquidity/volume floors but fails STRICT floors.
    result = build_canonical_observation(
        candidate=_candidate(liquidity="15000", volume="30000"),
        movement=_movement(change_5m="4", change_15m="6", change_1h="9", latest="0.50"),
        book=_book(timestamp=995.0),
        mode=ObservationMode.LIVE_STRICT,
    )
    assert isinstance(result, ObservationRejected)
    assert (
        OpportunityDiagnosticCode.LIQUIDITY_TOO_LOW in result.diagnostic_codes
        or OpportunityDiagnosticCode.VOLUME_TOO_LOW in result.diagnostic_codes
    )


def test_strict_live_eligible_includes_live_market_reason():
    result = build_canonical_observation(
        candidate=_candidate(liquidity="50000", volume="100000"),
        movement=_movement(change_5m="4", change_15m="6", change_1h="9", latest="0.50"),
        book=_book(timestamp=995.0, bid="0.497", ask="0.503", ask_size="5000"),
        mode=ObservationMode.LIVE_STRICT,
        confirmed_live_book=_book(
            timestamp=997.0, bid="0.497", ask="0.503", ask_size="5000"
        ),
    )
    assert isinstance(result, ObservationAccepted)
    assert OpportunityReasonCode.LIVE_MARKET in result.reason_codes
    assert result.score >= Decimal("75")


def test_recipient_settings_do_not_alter_canonical_score():
    book = _book()
    # Score API has no settings parameter; identical market inputs => identical score.
    from app.opportunity_intel.allocation import sweep_allocation_presets

    sweep = sweep_allocation_presets(book)
    score_a = compute_canonical_score(
        absolute_change_5m_pp=Decimal("4"),
        absolute_change_15m_pp=Decimal("6"),
        absolute_change_1h_pp=Decimal("8"),
        volume_24h_usd=Decimal("100000"),
        liquidity_usd=Decimal("50000"),
        visible_ask_depth_usd=Decimal(str(book.visible_ask_depth_usd)),
        canonical_execution=sweep.canonical,
        relative_spread=Decimal(str(book.spread / book.midpoint)),
        mode=ObservationMode.NORMAL,
        movement_trigger_5m_pp=Decimal("2"),
        movement_trigger_15m_pp=Decimal("3"),
        movement_trigger_1h_pp=Decimal("5"),
    )
    score_b = compute_canonical_score(
        absolute_change_5m_pp=Decimal("4"),
        absolute_change_15m_pp=Decimal("6"),
        absolute_change_1h_pp=Decimal("8"),
        volume_24h_usd=Decimal("100000"),
        liquidity_usd=Decimal("50000"),
        visible_ask_depth_usd=Decimal(str(book.visible_ask_depth_usd)),
        canonical_execution=sweep.canonical,
        relative_spread=Decimal(str(book.spread / book.midpoint)),
        mode=ObservationMode.NORMAL,
        movement_trigger_5m_pp=Decimal("2"),
        movement_trigger_15m_pp=Decimal("3"),
        movement_trigger_1h_pp=Decimal("5"),
    )
    assert score_a.score == score_b.score
    assert score_a.reason_codes == score_b.reason_codes
