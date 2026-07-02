from dataclasses import dataclass

from app.signals.confidence import calculate_basic_confidence
from app.signals.generator import WeatherSignal, generate_weather_signal_message
from app.signals.probability import estimate_probability_from_temperature_edge
from app.signals.validator import validate_weather_analysis_inputs
from app.weather.forecast import WeatherForecast
from app.weather.resolution import ResolutionDetails


@dataclass(frozen=True)
class WeatherComparisonInput:
    market_title: str
    resolution: ResolutionDetails
    forecast: WeatherForecast | None
    forecast_temperature_f: float | None
    market_price: float | None


def compare_weather_market(data: WeatherComparisonInput) -> WeatherSignal:
    validation = validate_weather_analysis_inputs(
        resolution=data.resolution,
        forecast=data.forecast,
    )

    if not validation.is_valid:
        message = "⚠️ WEATHER MARKET SKIPPED\n\nReasons:\n"
        message += "\n".join(f"• {reason}" for reason in validation.reasons)

        return WeatherSignal(
            decision="SKIP",
            probability=0.0,
            confidence_score=0.0,
            confidence_label="LOW",
            edge=None,
            message=message,
        )

    if data.forecast_temperature_f is None:
        return WeatherSignal(
            decision="SKIP",
            probability=0.0,
            confidence_score=0.0,
            confidence_label="LOW",
            edge=None,
            message="⚠️ WEATHER MARKET SKIPPED\n\nReason:\n• Missing forecast temperature.",
        )

    probability = estimate_probability_from_temperature_edge(
        forecast_temperature_f=data.forecast_temperature_f,
        threshold_f=data.resolution.threshold_f,
    )

    confidence_score = calculate_basic_confidence(
        has_station=bool(data.resolution.station_code),
        has_resolution_source=bool(data.resolution.source),
        has_forecast=data.forecast is not None,
        has_threshold=data.resolution.threshold_f is not None,
    )

    return generate_weather_signal_message(
        market_title=data.market_title,
        station_code=data.resolution.station_code,
        resolution_source=data.resolution.source,
        threshold_f=data.resolution.threshold_f,
        forecast_temperature_f=data.forecast_temperature_f,
        model_probability=probability,
        confidence_score=confidence_score,
        market_price=data.market_price,
    )
