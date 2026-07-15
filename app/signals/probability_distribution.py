"""Professional probability distribution across live Polymarket temperature bands."""

from __future__ import annotations

from dataclasses import dataclass

from app.polymarket.sibling_outcomes import OutcomeKind, SiblingOutcome, bucket_center
from app.signals.bucket_probability import OutcomeProbability, _normal_cdf, _prob_interval
from app.weather.temperature_units import TemperatureUnit

MIN_TAIL_PROB = 0.04
MAX_FAVORITE_HIGH_SPREAD = 0.30
MAX_FAVORITE_DEFAULT = 0.60
PREFERRED_CENTER_LOW_SPREAD = 0.50
BOUNDARY_SPLIT = 0.35
HIGH_SPREAD_F = 4.0
HIGH_SPREAD_C = 2.2


@dataclass(frozen=True)
class DistributionContext:
    consensus_native: float
    spread_native: float
    unit: TemperatureUnit
    thunderstorm_risk: bool = False
    pre_peak_cloud: bool = False


def _spread_threshold(unit: TemperatureUnit) -> float:
    return HIGH_SPREAD_F if unit == TemperatureUnit.FAHRENHEIT else HIGH_SPREAD_C


def _outcome_index(outcomes: list[SiblingOutcome], target: SiblingOutcome) -> int | None:
    for index, outcome in enumerate(outcomes):
        if outcome.market_id == target.market_id:
            return index
    return None


def _raw_gaussian_probs(
    outcomes: list[SiblingOutcome],
    mu: float,
    sigma: float,
) -> list[float]:
    raw: list[float] = []
    for outcome in outcomes:
        if outcome.kind == OutcomeKind.EXACT and outcome.exact_value is not None:
            prob = _prob_interval(outcome.exact_value, outcome.exact_value, mu, sigma)
        elif (
            outcome.kind == OutcomeKind.BRACKET
            and outcome.lower_bound is not None
            and outcome.upper_bound is not None
        ):
            prob = _prob_interval(
                outcome.lower_bound,
                outcome.upper_bound,
                mu,
                sigma,
            )
        elif outcome.kind == OutcomeKind.BELOW_TAIL and outcome.upper_bound is not None:
            prob = _normal_cdf(outcome.upper_bound + 0.5, mu, sigma)
        elif outcome.kind == OutcomeKind.ABOVE_TAIL and outcome.lower_bound is not None:
            prob = 1.0 - _normal_cdf(outcome.lower_bound - 0.5, mu, sigma)
        else:
            prob = 0.0
        raw.append(max(0.0, prob))
    return raw


def _nearest_brackets(
    outcomes: list[SiblingOutcome],
    consensus: float,
) -> tuple[int | None, int | None]:
    bracketed = [
        (index, item)
        for index, item in enumerate(outcomes)
        if item.kind == OutcomeKind.BRACKET
        and item.lower_bound is not None
        and item.upper_bound is not None
    ]
    if not bracketed:
        return None, None
    scored = sorted(
        bracketed,
        key=lambda pair: abs((bucket_center(pair[1]) or consensus) - consensus),
    )
    first = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else None
    return first, second


def distribute_market_probabilities(
    outcomes: list[SiblingOutcome],
    context: DistributionContext,
    *,
    sigma: float,
) -> list[OutcomeProbability]:
    if not outcomes:
        return []

    raw = _raw_gaussian_probs(outcomes, context.consensus_native, sigma)
    total = sum(raw)
    if total <= 0:
        return [OutcomeProbability(outcome=item, probability=0.0) for item in outcomes]

    probs = [value / total for value in raw]
    high_spread = context.spread_native >= _spread_threshold(context.unit)
    nearest_idx, runner_up_idx = _nearest_brackets(outcomes, context.consensus_native)

    if nearest_idx is not None:
        nearest = outcomes[nearest_idx]
        center = bucket_center(nearest) or context.consensus_native
        on_boundary = abs(center - context.consensus_native) <= 1.0
        if on_boundary and runner_up_idx is not None:
            probs[nearest_idx] = BOUNDARY_SPLIT
            probs[runner_up_idx] = BOUNDARY_SPLIT
        elif not high_spread:
            probs[nearest_idx] = max(probs[nearest_idx], PREFERRED_CENTER_LOW_SPREAD)
        else:
            probs[nearest_idx] = min(probs[nearest_idx], MAX_FAVORITE_HIGH_SPREAD)

    if context.thunderstorm_risk or context.pre_peak_cloud:
        for index, outcome in enumerate(outcomes):
            center = bucket_center(outcome)
            if center is not None and center > context.consensus_native:
                probs[index] *= 0.85

    cap = MAX_FAVORITE_HIGH_SPREAD if high_spread else MAX_FAVORITE_DEFAULT
    for index, value in enumerate(probs):
        if value > cap:
            probs[index] = cap

    for index, value in enumerate(probs):
        if value > 0 and value < MIN_TAIL_PROB:
            probs[index] = MIN_TAIL_PROB

    total = sum(probs)
    return [
        OutcomeProbability(outcome=outcome, probability=probs[index] / total)
        for index, outcome in enumerate(outcomes)
    ]
