"""Pure canonical observation builder for Checkpoint 3."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.copy_intel.order_book import ValidatedOrderBook
from app.opportunity_intel.allocation import (
    sweep_allocation_presets,
)
from app.opportunity_intel.book import BookAdapterFailure, BookAdapterSuccess, adapt_validated_book
from app.opportunity_intel.discovery import OutcomeCandidate, ResultStatus
from app.opportunity_intel.history import FreshnessStatus, MovementResult
from app.opportunity_intel.models import (
    CanonicalObservation,
    ExecutionMetrics,
    ObservationMode,
    OpportunityDiagnosticCode,
    OpportunityReasonCode,
)
from app.opportunity_intel.scoring import (
    SCORE_VERSION,
    CanonicalScoreResult,
    compute_canonical_score,
)
from app.opportunity_intel.settings import (
    BALANCED_MAXIMUM_ABSOLUTE_SPREAD,
    BALANCED_MAXIMUM_RELATIVE_SPREAD,
    BALANCED_MAXIMUM_SLIPPAGE,
    BALANCED_MINIMUM_24H_VOLUME_USD,
    BALANCED_MINIMUM_LIQUIDITY_USD,
    BALANCED_MINIMUM_SCORE,
    NORMAL_HISTORY_MIDPOINT_FLOOR,
    STRICT_LIVE_MAXIMUM_ABSOLUTE_SPREAD,
    STRICT_LIVE_MAXIMUM_RELATIVE_SPREAD,
    STRICT_LIVE_MAXIMUM_SLIPPAGE,
    STRICT_LIVE_MINIMUM_24H_VOLUME_USD,
    STRICT_LIVE_MINIMUM_LIQUIDITY_USD,
    STRICT_LIVE_MINIMUM_SCORE,
    STRICT_LIVE_HISTORY_MIDPOINT_FLOOR,
)

_MIDPOINT_MIN = Decimal("0.05")
_MIDPOINT_MAX = Decimal("0.95")

_NORMAL_MOVE_5M_PP = Decimal("2")
_NORMAL_MOVE_15M_PP = Decimal("3")
_NORMAL_MOVE_1H_PP = Decimal("5")
_STRICT_MOVE_5M_PP = Decimal("3")
_STRICT_MOVE_15M_PP = Decimal("5")
_STRICT_MOVE_1H_PP = Decimal("8")


@dataclass(frozen=True)
class ModeThresholds:
    minimum_score: Decimal
    minimum_liquidity_usd: Decimal
    minimum_24h_volume_usd: Decimal
    maximum_relative_spread: Decimal
    maximum_absolute_spread: Decimal
    maximum_slippage: Decimal
    history_midpoint_floor: Decimal
    move_5m_pp: Decimal
    move_15m_pp: Decimal
    move_1h_pp: Decimal


@dataclass(frozen=True)
class ObservationAccepted:
    observation: CanonicalObservation
    score: Decimal
    score_version: int
    reason_codes: tuple[OpportunityReasonCode, ...]
    allocation_metrics: tuple[ExecutionMetrics, ...]
    score_result: CanonicalScoreResult


@dataclass(frozen=True)
class ObservationRejected:
    diagnostic_codes: tuple[OpportunityDiagnosticCode, ...]
    score: Decimal | None = None
    score_version: int = SCORE_VERSION
    allocation_metrics: tuple[ExecutionMetrics, ...] = ()


ObservationBuildResult = ObservationAccepted | ObservationRejected


def thresholds_for_mode(mode: ObservationMode) -> ModeThresholds:
    if mode == ObservationMode.NORMAL:
        return ModeThresholds(
            minimum_score=Decimal(BALANCED_MINIMUM_SCORE),
            minimum_liquidity_usd=BALANCED_MINIMUM_LIQUIDITY_USD,
            minimum_24h_volume_usd=BALANCED_MINIMUM_24H_VOLUME_USD,
            maximum_relative_spread=BALANCED_MAXIMUM_RELATIVE_SPREAD,
            maximum_absolute_spread=BALANCED_MAXIMUM_ABSOLUTE_SPREAD,
            maximum_slippage=BALANCED_MAXIMUM_SLIPPAGE,
            history_midpoint_floor=NORMAL_HISTORY_MIDPOINT_FLOOR,
            move_5m_pp=_NORMAL_MOVE_5M_PP,
            move_15m_pp=_NORMAL_MOVE_15M_PP,
            move_1h_pp=_NORMAL_MOVE_1H_PP,
        )
    if mode == ObservationMode.LIVE_STRICT:
        return ModeThresholds(
            minimum_score=Decimal(STRICT_LIVE_MINIMUM_SCORE),
            minimum_liquidity_usd=STRICT_LIVE_MINIMUM_LIQUIDITY_USD,
            minimum_24h_volume_usd=STRICT_LIVE_MINIMUM_24H_VOLUME_USD,
            maximum_relative_spread=STRICT_LIVE_MAXIMUM_RELATIVE_SPREAD,
            maximum_absolute_spread=STRICT_LIVE_MAXIMUM_ABSOLUTE_SPREAD,
            maximum_slippage=STRICT_LIVE_MAXIMUM_SLIPPAGE,
            history_midpoint_floor=STRICT_LIVE_HISTORY_MIDPOINT_FLOOR,
            move_5m_pp=_STRICT_MOVE_5M_PP,
            move_15m_pp=_STRICT_MOVE_15M_PP,
            move_1h_pp=_STRICT_MOVE_1H_PP,
        )
    raise ValueError("unsupported observation mode")


def build_canonical_observation(
    *,
    candidate: OutcomeCandidate,
    movement: MovementResult,
    book: ValidatedOrderBook,
    mode: ObservationMode,
    confirmed_live_book: ValidatedOrderBook | None = None,
) -> ObservationBuildResult:
    """Combine candidate, movement, and one validated book into a typed result.

    ``confirmed_live_book`` is reserved for a later STRICT second-snapshot gate.
    When provided in LIVE_STRICT mode it becomes the scoring/execution snapshot;
    Checkpoint 3 does not orchestrate the confirmation fetch.
    """
    diagnostics: list[OpportunityDiagnosticCode] = []

    if candidate.asset_id != movement.asset_id:
        return ObservationRejected(
            diagnostic_codes=(OpportunityDiagnosticCode.IDENTITY_INVALID,)
        )
    if book.asset_id != candidate.asset_id:
        return ObservationRejected(
            diagnostic_codes=(OpportunityDiagnosticCode.BOOK_ASSET_MISMATCH,)
        )

    active_book = book
    if mode == ObservationMode.LIVE_STRICT and confirmed_live_book is not None:
        if confirmed_live_book.asset_id != candidate.asset_id:
            return ObservationRejected(
                diagnostic_codes=(OpportunityDiagnosticCode.BOOK_ASSET_MISMATCH,)
            )
        active_book = confirmed_live_book

    adapted = adapt_validated_book(
        active_book,
        requested_asset=candidate.asset_id,
        mode=mode,
    )
    if isinstance(adapted, BookAdapterFailure):
        return ObservationRejected(diagnostic_codes=(adapted.diagnostic_code,))
    assert isinstance(adapted, BookAdapterSuccess)

    if (
        movement.status != ResultStatus.COMPLETE
        or movement.freshness != FreshnessStatus.FRESH
        or movement.latest_price is None
        or movement.latest_at is None
        or movement.absolute_change_5m_pp is None
        or movement.absolute_change_15m_pp is None
        or movement.absolute_change_1h_pp is None
        or movement.change_5m_pp is None
        or movement.change_15m_pp is None
        or movement.change_1h_pp is None
    ):
        code = movement.diagnostic_code or OpportunityDiagnosticCode.HISTORY_MISSING
        return ObservationRejected(diagnostic_codes=(code,))

    thresholds = thresholds_for_mode(mode)
    agreement_limit = max(thresholds.history_midpoint_floor, adapted.absolute_spread)
    if abs(movement.latest_price - adapted.midpoint) > agreement_limit:
        diagnostics.append(OpportunityDiagnosticCode.HISTORY_BOOK_MISMATCH)

    if not (_MIDPOINT_MIN <= adapted.midpoint <= _MIDPOINT_MAX):
        diagnostics.append(OpportunityDiagnosticCode.MIDPOINT_OUT_OF_BOUNDS)
    if candidate.liquidity_usd < thresholds.minimum_liquidity_usd:
        diagnostics.append(OpportunityDiagnosticCode.LIQUIDITY_TOO_LOW)
    if candidate.volume_24h_usd < thresholds.minimum_24h_volume_usd:
        diagnostics.append(OpportunityDiagnosticCode.VOLUME_TOO_LOW)
    if adapted.relative_spread > thresholds.maximum_relative_spread:
        diagnostics.append(OpportunityDiagnosticCode.SPREAD_TOO_WIDE)
    if adapted.absolute_spread > thresholds.maximum_absolute_spread:
        diagnostics.append(OpportunityDiagnosticCode.SPREAD_TOO_WIDE)

    move_ok = (
        movement.absolute_change_5m_pp >= thresholds.move_5m_pp
        or movement.absolute_change_15m_pp >= thresholds.move_15m_pp
        or movement.absolute_change_1h_pp >= thresholds.move_1h_pp
    )
    if not move_ok:
        diagnostics.append(OpportunityDiagnosticCode.MOVEMENT_TRIGGER_MISSING)

    sweep = sweep_allocation_presets(active_book, observed_at=adapted.book_at)
    canonical = sweep.canonical
    if not canonical.fully_executable or canonical.slippage is None:
        diagnostics.append(OpportunityDiagnosticCode.INSUFFICIENT_DEPTH)
    elif canonical.slippage > thresholds.maximum_slippage:
        diagnostics.append(OpportunityDiagnosticCode.SLIPPAGE_TOO_HIGH)

    if diagnostics:
        return ObservationRejected(
            diagnostic_codes=tuple(dict.fromkeys(diagnostics)),
            allocation_metrics=sweep.metrics,
        )

    assert canonical.fully_executable and canonical.slippage is not None
    score_result = compute_canonical_score(
        absolute_change_5m_pp=movement.absolute_change_5m_pp,
        absolute_change_15m_pp=movement.absolute_change_15m_pp,
        absolute_change_1h_pp=movement.absolute_change_1h_pp,
        volume_24h_usd=candidate.volume_24h_usd,
        liquidity_usd=candidate.liquidity_usd,
        visible_ask_depth_usd=adapted.visible_ask_depth_usd,
        canonical_execution=canonical,
        relative_spread=adapted.relative_spread,
        mode=mode,
        movement_trigger_5m_pp=thresholds.move_5m_pp,
        movement_trigger_15m_pp=thresholds.move_15m_pp,
        movement_trigger_1h_pp=thresholds.move_1h_pp,
    )
    if score_result.score < thresholds.minimum_score:
        return ObservationRejected(
            diagnostic_codes=(OpportunityDiagnosticCode.SCORE_BELOW_FLOOR,),
            score=score_result.score,
            score_version=score_result.score_version,
            allocation_metrics=sweep.metrics,
        )
    if not score_result.reason_codes:
        return ObservationRejected(
            diagnostic_codes=(OpportunityDiagnosticCode.MOVEMENT_TRIGGER_MISSING,),
            score=score_result.score,
            score_version=score_result.score_version,
            allocation_metrics=sweep.metrics,
        )

    observed_at = adapted.book_at
    observation = CanonicalObservation(
        asset_id=candidate.asset_id,
        condition_id=candidate.condition_id,
        event_id=candidate.event_id,
        market_id=candidate.market_id,
        event_slug=candidate.event_slug,
        question=candidate.question,
        outcome=candidate.outcome,
        category_ids=candidate.category_ids,
        mode=mode,
        observed_at=observed_at,
        metadata_at=candidate.metadata_at,
        history_at=movement.latest_at,
        book_at=adapted.book_at,
        best_bid=adapted.best_bid,
        best_ask=adapted.best_ask,
        midpoint=adapted.midpoint,
        absolute_spread=adapted.absolute_spread,
        relative_spread=adapted.relative_spread,
        liquidity_usd=candidate.liquidity_usd,
        volume_24h_usd=candidate.volume_24h_usd,
        move_5m=movement.change_5m_pp / Decimal("100"),
        move_15m=movement.change_15m_pp / Decimal("100"),
        move_1h=movement.change_1h_pp / Decimal("100"),
        visible_bid_depth_usd=adapted.visible_bid_depth_usd,
        visible_ask_depth_usd=adapted.visible_ask_depth_usd,
        canonical_execution=canonical,
    )
    return ObservationAccepted(
        observation=observation,
        score=score_result.score,
        score_version=score_result.score_version,
        reason_codes=score_result.reason_codes,
        allocation_metrics=sweep.metrics,
        score_result=score_result,
    )
