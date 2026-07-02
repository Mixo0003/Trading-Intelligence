from dataclasses import dataclass

from app.signals.confidence import confidence_label


@dataclass(frozen=True)
class WeatherSignal:
    decision: str
    probability: float
    confidence_score: float
    confidence_label: str
    edge: float | None
    message: str


def format_percent(value: float) -> str:
    return f"{value * 100:.0f}%"


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
    edge = None
    if market_price is not None:
        edge = model_probability - market_price

    label = confidence_label(confidence_score)

    if edge is not None and edge >= 0.08 and confidence_score >= 0.55:
        decision = "POSSIBLE VALUE"
    elif confidence_score < 0.55:
        decision = "WAIT / LOW CONFIDENCE"
    else:
        decision = "NO CLEAR EDGE"

    edge_text = "N/A" if edge is None else f"{edge * 100:+.0f}%"
    market_price_text = "N/A" if market_price is None else format_percent(market_price)

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

Model Probability:
{format_percent(model_probability)}

Estimated Edge:
{edge_text}

Confidence:
{label}

Decision:
{decision}

Rule:
Station must come from the market description/rules, not from city center.
"""

    return WeatherSignal(
        decision=decision,
        probability=model_probability,
        confidence_score=confidence_score,
        confidence_label=label,
        edge=edge,
        message=message,
    )
