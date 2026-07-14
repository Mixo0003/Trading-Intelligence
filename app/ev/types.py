from dataclasses import dataclass
from enum import Enum


class RecommendationTier(str, Enum):
    SKIP = "SKIP"
    WATCH = "WATCH"
    SMALL_BET = "SMALL BET"
    NORMAL_BET = "NORMAL BET"
    STRONG_OPPORTUNITY = "STRONG OPPORTUNITY"


class ConfidenceLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class MarketCategory(str, Enum):
    DEFAULT = "default"
    WEATHER = "weather"


@dataclass(frozen=True)
class RecommendationResult:
    tier: RecommendationTier
    model_probability: float
    market_probability: float
    edge: float
    ev: float
    kelly_fraction: float
    max_bankroll_fraction: float
