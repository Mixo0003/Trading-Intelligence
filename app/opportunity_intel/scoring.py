"""Deterministic canonical score version 1 for Market Opportunity Scanner."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from math import log

from app.opportunity_intel.allocation import CANONICAL_ALLOCATION_USD
from app.opportunity_intel.models import (
    ExecutionMetrics,
    ObservationMode,
    OpportunityReasonCode,
)

SCORE_VERSION = 1
SCORE_QUANTUM = Decimal("0.1")

_MOVEMENT_5M_MAX = Decimal("10")
_MOVEMENT_5M_SCALE_PP = Decimal("3")
_MOVEMENT_15M_MAX = Decimal("15")
_MOVEMENT_15M_SCALE_PP = Decimal("5")
_MOVEMENT_1H_MAX = Decimal("20")
_MOVEMENT_1H_SCALE_PP = Decimal("10")

_VOLUME_MAX = Decimal("15")
_VOLUME_FLOOR = Decimal("25000")
_VOLUME_CEILING = Decimal("1000000")

_LIQUIDITY_MAX = Decimal("15")
_LIQUIDITY_FLOOR = Decimal("10000")
_LIQUIDITY_CEILING = Decimal("500000")

_DEPTH_MAX = Decimal("10")
_DEPTH_MULTIPLIER = Decimal("5")

_SLIPPAGE_MAX = Decimal("10")
_SLIPPAGE_ZERO_AT = Decimal("0.02")

_TIGHTNESS_MAX = Decimal("5")
_TIGHTNESS_ZERO_AT = Decimal("0.10")

# Reason-code contribution floors (component points).
_VOLUME_REASON_FLOOR = Decimal("5")
_DEPTH_REASON_FLOOR = Decimal("5")
_SLIPPAGE_REASON_FLOOR = Decimal("5")
_TIGHTNESS_REASON_FLOOR = Decimal("2.5")


@dataclass(frozen=True)
class ScoreComponents:
    movement_5m: Decimal
    movement_15m: Decimal
    movement_1h: Decimal
    volume: Decimal
    liquidity: Decimal
    depth: Decimal
    slippage: Decimal
    tightness: Decimal

    @property
    def total_raw(self) -> Decimal:
        return (
            self.movement_5m
            + self.movement_15m
            + self.movement_1h
            + self.volume
            + self.liquidity
            + self.depth
            + self.slippage
            + self.tightness
        )


@dataclass(frozen=True)
class CanonicalScoreResult:
    score: Decimal
    score_version: int
    components: ScoreComponents
    reason_codes: tuple[OpportunityReasonCode, ...]


def round_score(value: Decimal) -> Decimal:
    """Persistable score rounding: one decimal place, half-up."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("score must be a finite Decimal")
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        quantized = value.quantize(SCORE_QUANTUM)
    if quantized < 0:
        return Decimal("0.0")
    if quantized > Decimal("100.0"):
        return Decimal("100.0")
    return quantized


def _clamp_unit(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    if value >= 1:
        return Decimal("1")
    return value


def _scaled_component(magnitude: Decimal, *, maximum: Decimal, scale: Decimal) -> Decimal:
    if magnitude <= 0:
        return Decimal("0")
    return maximum * _clamp_unit(magnitude / scale)


def _log_component(
    value: Decimal,
    *,
    maximum: Decimal,
    floor: Decimal,
    ceiling: Decimal,
) -> Decimal:
    if value <= floor:
        return Decimal("0")
    if value >= ceiling:
        return maximum
    # Decimal-safe via float log of exact ratios; result re-decimalized.
    numerator = Decimal(str(log(float(value / floor))))
    denominator = Decimal(str(log(float(ceiling / floor))))
    return maximum * _clamp_unit(numerator / denominator)


def compute_score_components(
    *,
    absolute_change_5m_pp: Decimal,
    absolute_change_15m_pp: Decimal,
    absolute_change_1h_pp: Decimal,
    volume_24h_usd: Decimal,
    liquidity_usd: Decimal,
    visible_ask_depth_usd: Decimal,
    canonical_slippage: Decimal,
    relative_spread: Decimal,
    canonical_allocation_usd: Decimal = CANONICAL_ALLOCATION_USD,
) -> ScoreComponents:
    if canonical_allocation_usd != CANONICAL_ALLOCATION_USD:
        raise ValueError("canonical allocation is fixed at 100 USD for score version 1")
    depth_target = _DEPTH_MULTIPLIER * canonical_allocation_usd
    slippage_for_score = canonical_slippage if canonical_slippage > 0 else Decimal("0")
    return ScoreComponents(
        movement_5m=_scaled_component(
            absolute_change_5m_pp,
            maximum=_MOVEMENT_5M_MAX,
            scale=_MOVEMENT_5M_SCALE_PP,
        ),
        movement_15m=_scaled_component(
            absolute_change_15m_pp,
            maximum=_MOVEMENT_15M_MAX,
            scale=_MOVEMENT_15M_SCALE_PP,
        ),
        movement_1h=_scaled_component(
            absolute_change_1h_pp,
            maximum=_MOVEMENT_1H_MAX,
            scale=_MOVEMENT_1H_SCALE_PP,
        ),
        volume=_log_component(
            volume_24h_usd,
            maximum=_VOLUME_MAX,
            floor=_VOLUME_FLOOR,
            ceiling=_VOLUME_CEILING,
        ),
        liquidity=_log_component(
            liquidity_usd,
            maximum=_LIQUIDITY_MAX,
            floor=_LIQUIDITY_FLOOR,
            ceiling=_LIQUIDITY_CEILING,
        ),
        depth=_scaled_component(
            visible_ask_depth_usd,
            maximum=_DEPTH_MAX,
            scale=depth_target,
        ),
        slippage=_SLIPPAGE_MAX
        * (Decimal("1") - _clamp_unit(slippage_for_score / _SLIPPAGE_ZERO_AT)),
        tightness=_TIGHTNESS_MAX
        * (Decimal("1") - _clamp_unit(relative_spread / _TIGHTNESS_ZERO_AT)),
    )


def compute_canonical_score(
    *,
    absolute_change_5m_pp: Decimal,
    absolute_change_15m_pp: Decimal,
    absolute_change_1h_pp: Decimal,
    volume_24h_usd: Decimal,
    liquidity_usd: Decimal,
    visible_ask_depth_usd: Decimal,
    canonical_execution: ExecutionMetrics,
    relative_spread: Decimal,
    mode: ObservationMode,
    movement_trigger_5m_pp: Decimal,
    movement_trigger_15m_pp: Decimal,
    movement_trigger_1h_pp: Decimal,
) -> CanonicalScoreResult:
    """Score version 1. Recipient settings must not be passed into this function."""
    if canonical_execution.allocation_usd != CANONICAL_ALLOCATION_USD:
        raise ValueError("canonical execution must use the 100 USD Balanced preset")
    if not canonical_execution.fully_executable or canonical_execution.slippage is None:
        raise ValueError("canonical score requires a fully executable 100 USD sweep")

    components = compute_score_components(
        absolute_change_5m_pp=absolute_change_5m_pp,
        absolute_change_15m_pp=absolute_change_15m_pp,
        absolute_change_1h_pp=absolute_change_1h_pp,
        volume_24h_usd=volume_24h_usd,
        liquidity_usd=liquidity_usd,
        visible_ask_depth_usd=visible_ask_depth_usd,
        canonical_slippage=canonical_execution.slippage,
        relative_spread=relative_spread,
    )
    score = round_score(components.total_raw)
    reasons = _reason_codes(
        mode=mode,
        absolute_change_5m_pp=absolute_change_5m_pp,
        absolute_change_15m_pp=absolute_change_15m_pp,
        absolute_change_1h_pp=absolute_change_1h_pp,
        movement_trigger_5m_pp=movement_trigger_5m_pp,
        movement_trigger_15m_pp=movement_trigger_15m_pp,
        movement_trigger_1h_pp=movement_trigger_1h_pp,
        components=components,
    )
    return CanonicalScoreResult(
        score=score,
        score_version=SCORE_VERSION,
        components=components,
        reason_codes=reasons,
    )


def _reason_codes(
    *,
    mode: ObservationMode,
    absolute_change_5m_pp: Decimal,
    absolute_change_15m_pp: Decimal,
    absolute_change_1h_pp: Decimal,
    movement_trigger_5m_pp: Decimal,
    movement_trigger_15m_pp: Decimal,
    movement_trigger_1h_pp: Decimal,
    components: ScoreComponents,
) -> tuple[OpportunityReasonCode, ...]:
    codes: list[OpportunityReasonCode] = []
    if absolute_change_5m_pp >= movement_trigger_5m_pp:
        codes.append(OpportunityReasonCode.FAST_5M_MOVE)
    if absolute_change_15m_pp >= movement_trigger_15m_pp:
        codes.append(OpportunityReasonCode.SUSTAINED_15M_MOVE)
    if absolute_change_1h_pp >= movement_trigger_1h_pp:
        codes.append(OpportunityReasonCode.SUSTAINED_1H_MOVE)
    if components.volume >= _VOLUME_REASON_FLOOR:
        codes.append(OpportunityReasonCode.HIGH_24H_VOLUME)
    if components.depth >= _DEPTH_REASON_FLOOR:
        codes.append(OpportunityReasonCode.DEEP_EXECUTABLE_BOOK)
    if components.slippage >= _SLIPPAGE_REASON_FLOOR:
        codes.append(OpportunityReasonCode.LOW_SLIPPAGE)
    if components.tightness >= _TIGHTNESS_REASON_FLOOR:
        codes.append(OpportunityReasonCode.TIGHT_SPREAD)
    if mode == ObservationMode.LIVE_STRICT:
        codes.append(OpportunityReasonCode.LIVE_MARKET)
    # Stable deterministic order matching enum declaration order among selected.
    order = {code: index for index, code in enumerate(OpportunityReasonCode)}
    return tuple(sorted(set(codes), key=lambda code: order[code]))
