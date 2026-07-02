from dataclasses import dataclass

from app.weather.resolution import ResolutionDetails
from app.weather.forecast import WeatherForecast


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    reasons: list[str]


def validate_weather_analysis_inputs(
    resolution: ResolutionDetails,
    forecast: WeatherForecast | None,
) -> ValidationResult:
    reasons: list[str] = []

    if not resolution.source:
        reasons.append("Missing resolution source.")

    if not resolution.station_code:
        reasons.append("Missing station code.")

    if not resolution.metric:
        reasons.append("Missing temperature metric.")

    if resolution.threshold_f is None:
        reasons.append("Missing temperature threshold.")

    if not resolution.target_date:
        reasons.append("Missing target date.")

    if forecast is None:
        reasons.append("Missing weather forecast.")

    return ValidationResult(
        is_valid=len(reasons) == 0,
        reasons=reasons,
    )
