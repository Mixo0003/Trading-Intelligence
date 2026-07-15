"""Coherent sibling-outcome probability distribution."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.polymarket.sibling_outcomes import OutcomeKind, SiblingOutcome
from app.weather.temperature_units import TemperatureUnit

# Conservative default uncertainty (documented, unvalidated).
DEFAULT_FORECAST_SIGMA_C = 2.0
DEFAULT_FORECAST_SIGMA_F = DEFAULT_FORECAST_SIGMA_C * 9.0 / 5.0

# Whole-degree continuity: integer outcomes map to ±0.5 half-degree boundaries.
HALF_DEGREE_OFFSET = 0.5

# 2°F bracket width used for high-probability guard.
BRACKET_WIDTH_F = 2.0
BRACKET_WIDTH_C = 2.0


def default_sigma(unit: TemperatureUnit) -> float:
    if unit == TemperatureUnit.FAHRENHEIT:
        return DEFAULT_FORECAST_SIGMA_F
    return DEFAULT_FORECAST_SIGMA_C


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def _prob_interval(low: float, high: float, mu: float, sigma: float) -> float:
    return max(
        0.0,
        _normal_cdf(high + HALF_DEGREE_OFFSET, mu, sigma)
        - _normal_cdf(low - HALF_DEGREE_OFFSET, mu, sigma),
    )


def _prob_exact_degree(value: float, mu: float, sigma: float) -> float:
    return _prob_interval(value, value, mu, sigma)


@dataclass(frozen=True)
class OutcomeProbability:
    outcome: SiblingOutcome
    probability: float


def compute_sibling_probabilities(
    outcomes: list[SiblingOutcome],
    forecast_value: float | None = None,
    *,
    unit: TemperatureUnit | None = None,
    sigma: float | None = None,
    forecast_c: float | None = None,
) -> list[OutcomeProbability]:
    """Compute normalized sibling probabilities in the market's native unit.

    ``forecast_c`` is accepted for backward compatibility; prefer ``forecast_value``
    with an explicit ``unit``.
    """
    if forecast_value is None:
        forecast_value = forecast_c
    if forecast_value is None:
        return [OutcomeProbability(outcome=item, probability=0.0) for item in outcomes]

    resolved_unit = unit or (outcomes[0].unit if outcomes else TemperatureUnit.CELSIUS)
    resolved_sigma = sigma if sigma is not None else default_sigma(resolved_unit)

    raw_probs: list[tuple[SiblingOutcome, float]] = []
    for outcome in outcomes:
        if outcome.kind == OutcomeKind.EXACT and outcome.exact_value is not None:
            prob = _prob_exact_degree(outcome.exact_value, forecast_value, resolved_sigma)
        elif (
            outcome.kind == OutcomeKind.BRACKET
            and outcome.lower_bound is not None
            and outcome.upper_bound is not None
        ):
            prob = _prob_interval(
                outcome.lower_bound,
                outcome.upper_bound,
                forecast_value,
                resolved_sigma,
            )
        elif outcome.kind == OutcomeKind.BELOW_TAIL and outcome.upper_bound is not None:
            prob = _normal_cdf(
                outcome.upper_bound + HALF_DEGREE_OFFSET,
                forecast_value,
                resolved_sigma,
            )
        elif outcome.kind == OutcomeKind.ABOVE_TAIL and outcome.lower_bound is not None:
            prob = 1.0 - _normal_cdf(
                outcome.lower_bound - HALF_DEGREE_OFFSET,
                forecast_value,
                resolved_sigma,
            )
        else:
            prob = 0.0
        raw_probs.append((outcome, max(0.0, prob)))

    total = sum(prob for _, prob in raw_probs)
    if total <= 0:
        return [OutcomeProbability(outcome=item, probability=0.0) for item, _ in raw_probs]

    return [
        OutcomeProbability(outcome=item, probability=prob / total)
        for item, prob in raw_probs
    ]


def expected_range(
    forecast_value: float,
    sigma: float | None = None,
    *,
    unit: TemperatureUnit | None = None,
) -> tuple[int, int]:
    resolved_sigma = sigma if sigma is not None else default_sigma(unit or TemperatureUnit.CELSIUS)
    low = int(math.floor(forecast_value - resolved_sigma))
    high = int(math.ceil(forecast_value + resolved_sigma))
    return low, high


def expected_range_c(forecast_c: float, sigma_c: float = DEFAULT_FORECAST_SIGMA_C) -> tuple[int, int]:
    return expected_range(forecast_c, sigma_c, unit=TemperatureUnit.CELSIUS)
