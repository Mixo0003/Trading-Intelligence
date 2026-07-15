from dataclasses import dataclass

from app.ev import (
    MarketCategory,
    determine_recommendation_tier,
)
from app.ev.types import RecommendationTier
from app.signals.confidence import confidence_label, confidence_score_to_level


@dataclass(frozen=True)
class WeatherSignal:
    decision: str
    probability: float
    confidence_score: float
    confidence_label: str
    edge: float | None
    ev: float | None
    kelly_fraction: float | None
    recommendation_tier: str | None
    market_probability: float | None
    message: str
    forecast_temperature_f: float | None = None


def format_percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def _format_signed_percent(value: float) -> str:
    return f"{value * 100:+.0f}%"


def generate_weather_signal_message(
    market_title: str,
    station_code: str,
    resolution_source: str,
    threshold_f: float,
    forecast_temperature_f: float,
    model_probability: float,
    confidence_score: float,
    market_price: float | None = None,
) -> WeatherSignal:
    label = confidence_label(confidence_score)
    confidence_level = confidence_score_to_level(confidence_score)

    edge = None
    ev = None
    kelly_fraction = None
    market_probability = None
    recommendation_tier = RecommendationTier.WATCH.value

    if market_price is not None:
        result = determine_recommendation_tier(
            model_probability,
            market_price,
            confidence_level,
            market_category=MarketCategory.WEATHER,
        )
        edge = result.edge
        ev = result.ev
        kelly_fraction = result.kelly_fraction
        market_probability = result.market_probability
        recommendation_tier = result.tier.value

    decision = recommendation_tier

    edge_text = "N/A" if edge is None else _format_signed_percent(edge)
    ev_text = "N/A" if ev is None else _format_signed_percent(ev)
    kelly_text = "N/A" if kelly_fraction is None else format_percent(kelly_fraction)
    market_price_text = "N/A" if market_price is None else format_percent(market_price)
    market_probability_text = (
        "N/A" if market_probability is None else format_percent(market_probability)
    )

    message = f"""🚨 WEATHER MARKET SIGNAL

Market:
{market_title}

Resolution:
{resolution_source} / {station_code}

Threshold:
{threshold_f:.1f}°F

Forecast:
{forecast_temperature_f:.1f}°F

Market Price:
{market_price_text}

Implied Probability:
{market_probability_text}

Model Probability:
{format_percent(model_probability)}

Estimated Edge:
{edge_text}

EV:
{ev_text}

Kelly Fraction:
{kelly_text}

Confidence:
{label}

Recommendation:
{recommendation_tier}

Rule:
Station must come from the market description/rules, not from city center.
"""

    return WeatherSignal(
        decision=decision,
        probability=model_probability,
        confidence_score=confidence_score,
        confidence_label=label,
        edge=edge,
        ev=ev,
        kelly_fraction=kelly_fraction,
        recommendation_tier=recommendation_tier,
        market_probability=market_probability,
        message=message,
        forecast_temperature_f=forecast_temperature_f,
    )
