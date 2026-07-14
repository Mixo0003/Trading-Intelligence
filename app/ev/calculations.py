from app.ev.exceptions import (
    InvalidDecimalOddsError,
    InvalidKellyFractionError,
    InvalidMarketPriceError,
    InvalidProbabilityError,
)
from app.ev.policy import MAX_BANKROLL_DEFAULT


def _validate_probability(value: float, *, name: str = "probability") -> None:
    if not 0.0 <= value <= 1.0:
        raise InvalidProbabilityError(f"{name} must be between 0 and 1, got {value}")


def _validate_market_price(value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise InvalidMarketPriceError(
            f"market price must be between 0 and 1, got {value}"
        )


def prediction_market_price_to_probability(price: float) -> float:
    """Convert a prediction-market YES price (0–1) to implied probability."""
    _validate_market_price(price)
    return price


def decimal_odds_to_probability(decimal_odds: float) -> float:
    """Convert decimal odds (> 1) to implied probability."""
    if decimal_odds <= 1.0:
        raise InvalidDecimalOddsError(
            f"decimal odds must be greater than 1, got {decimal_odds}"
        )
    return 1.0 / decimal_odds


def calculate_edge(model_probability: float, market_probability: float) -> float:
    """Edge = model probability minus market implied probability."""
    _validate_probability(model_probability, name="model_probability")
    _validate_probability(market_probability, name="market_probability")
    return model_probability - market_probability


def calculate_binary_ev(model_probability: float, market_price: float) -> float:
    """Expected value per YES share bought at market_price."""
    _validate_probability(model_probability, name="model_probability")
    _validate_market_price(market_price)
    return (
        model_probability * (1.0 - market_price)
        - (1.0 - model_probability) * market_price
    )


def _full_kelly_fraction(model_probability: float, market_price: float) -> float:
    if market_price >= 1.0:
        return 0.0
    raw = (model_probability - market_price) / (1.0 - market_price)
    return max(0.0, raw)


def calculate_fractional_kelly(
    model_probability: float,
    market_price: float,
    fraction: float,
    *,
    max_bankroll_fraction: float | None = None,
) -> float:
    """Fractional Kelly position size, capped by risk policy. Never negative."""
    _validate_probability(model_probability, name="model_probability")
    _validate_market_price(market_price)
    if not 0.0 <= fraction <= 1.0:
        raise InvalidKellyFractionError(
            f"Kelly fraction must be between 0 and 1, got {fraction}"
        )

    cap = MAX_BANKROLL_DEFAULT if max_bankroll_fraction is None else max_bankroll_fraction
    if cap < 0.0:
        raise ValueError(f"max_bankroll_fraction must be non-negative, got {cap}")

    full_kelly = _full_kelly_fraction(model_probability, market_price)
    position = full_kelly * fraction
    return min(max(0.0, position), cap)
