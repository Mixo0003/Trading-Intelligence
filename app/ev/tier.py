from app.ev.calculations import (
    calculate_binary_ev,
    calculate_edge,
    calculate_fractional_kelly,
    prediction_market_price_to_probability,
)
from app.ev.exceptions import (
    InvalidMarketPriceError,
    InvalidProbabilityError,
)
from app.ev.policy import (
    DEFAULT_KELLY_FRACTION,
    EV_THRESHOLD_NORMAL_BET,
    EV_THRESHOLD_SMALL_BET,
    EV_THRESHOLD_STRONG_OPPORTUNITY,
    EV_THRESHOLD_WATCH,
    MAX_BANKROLL_DEFAULT,
    MAX_BANKROLL_LOW_CONFIDENCE,
    MAX_BANKROLL_WEATHER,
)
from app.ev.types import (
    ConfidenceLevel,
    MarketCategory,
    RecommendationResult,
    RecommendationTier,
)


def _resolve_bankroll_cap(
    confidence: ConfidenceLevel,
    market_category: MarketCategory,
) -> float:
    if confidence == ConfidenceLevel.LOW:
        return MAX_BANKROLL_LOW_CONFIDENCE
    if market_category == MarketCategory.WEATHER:
        return MAX_BANKROLL_WEATHER
    return MAX_BANKROLL_DEFAULT


def _base_tier_from_ev(ev: float) -> RecommendationTier:
    if ev < EV_THRESHOLD_WATCH:
        return RecommendationTier.WATCH
    if ev < EV_THRESHOLD_NORMAL_BET:
        return RecommendationTier.SMALL_BET
    if ev < EV_THRESHOLD_STRONG_OPPORTUNITY:
        return RecommendationTier.NORMAL_BET
    return RecommendationTier.STRONG_OPPORTUNITY


def _downgrade_tier(tier: RecommendationTier) -> RecommendationTier:
    downgrades = {
        RecommendationTier.STRONG_OPPORTUNITY: RecommendationTier.NORMAL_BET,
        RecommendationTier.NORMAL_BET: RecommendationTier.SMALL_BET,
        RecommendationTier.SMALL_BET: RecommendationTier.WATCH,
        RecommendationTier.WATCH: RecommendationTier.WATCH,
        RecommendationTier.SKIP: RecommendationTier.SKIP,
    }
    return downgrades[tier]


def determine_recommendation_tier(
    model_probability: float,
    market_price: float,
    confidence: ConfidenceLevel,
    *,
    market_category: MarketCategory = MarketCategory.DEFAULT,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
) -> RecommendationResult:
    """Determine recommendation tier from EV, edge, confidence, and risk policy."""
    try:
        market_probability = prediction_market_price_to_probability(market_price)
        edge = calculate_edge(model_probability, market_probability)
        ev = calculate_binary_ev(model_probability, market_price)
    except (InvalidProbabilityError, InvalidMarketPriceError):
        return RecommendationResult(
            tier=RecommendationTier.SKIP,
            model_probability=model_probability,
            market_probability=0.0,
            edge=0.0,
            ev=0.0,
            kelly_fraction=0.0,
            max_bankroll_fraction=MAX_BANKROLL_DEFAULT,
        )

    max_bankroll = _resolve_bankroll_cap(confidence, market_category)
    kelly = calculate_fractional_kelly(
        model_probability,
        market_price,
        kelly_fraction,
        max_bankroll_fraction=max_bankroll,
    )

    tier = _base_tier_from_ev(ev)
    if confidence == ConfidenceLevel.LOW:
        tier = _downgrade_tier(tier)

    return RecommendationResult(
        tier=tier,
        model_probability=model_probability,
        market_probability=market_probability,
        edge=edge,
        ev=ev,
        kelly_fraction=kelly,
        max_bankroll_fraction=max_bankroll,
    )
