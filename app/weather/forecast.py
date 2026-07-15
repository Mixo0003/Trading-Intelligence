from dataclasses import dataclass
from typing import Any

import requests

from app.config.settings import Settings
from app.metrics.scan_metrics import active_scan_metrics
from app.weather.stations import WeatherStation


def _track_weather_request() -> None:
    metrics = active_scan_metrics()
    if metrics is not None:
        metrics.weather_requests += 1


@dataclass(frozen=True)
class WeatherForecast:
    station_code: str
    station_name: str
    source: str
    forecast_text: str
    raw: dict[str, Any]


def _fetch_open_meteo_forecast(station: WeatherStation) -> WeatherForecast:
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": station.latitude,
        "longitude": station.longitude,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "forecast_days": 7,
        "timezone": "auto",
    }

    response = requests.get(url, params=params, timeout=20)
    _track_weather_request()
    response.raise_for_status()
    data = response.json()

    daily = data.get("daily", {}) or {}
    dates = daily.get("time", []) or []
    max_temps = daily.get("temperature_2m_max", []) or []
    min_temps = daily.get("temperature_2m_min", []) or []
    rain_probs = daily.get("precipitation_probability_max", []) or []
    rain_amounts = daily.get("precipitation_sum", []) or []
    wind_speeds = daily.get("wind_speed_10m_max", []) or []

    lines = []
    for i, day in enumerate(dates[:7]):
        high = max_temps[i] if i < len(max_temps) else None
        low = min_temps[i] if i < len(min_temps) else None
        rain_prob = rain_probs[i] if i < len(rain_probs) else None
        rain_amount = rain_amounts[i] if i < len(rain_amounts) else None
        wind = wind_speeds[i] if i < len(wind_speeds) else None

        lines.append(
            f"{day}: high={high}F, low={low}F, "
            f"precip_prob={rain_prob}%, precip={rain_amount}in, wind={wind}mph"
        )

    forecast_text = "\n".join(lines)

    return WeatherForecast(
        station_code=station.code,
        station_name=station.name,
        source="open_meteo",
        forecast_text=forecast_text,
        raw=data,
    )


def fetch_weather_gov_forecast(
    settings: Settings,
    station: WeatherStation,
) -> WeatherForecast:
    headers = {
        "User-Agent": "Trading Intelligence Bot contact@example.com",
        "Accept": "application/geo+json, application/json",
    }

    try:
        points_url = f"{settings.weather_gov_base_url}/points/{station.latitude},{station.longitude}"
        points_response = requests.get(points_url, headers=headers, timeout=20)
        _track_weather_request()
        points_response.raise_for_status()
        points_data = points_response.json()

        forecast_url = points_data["properties"]["forecast"]
        forecast_response = requests.get(forecast_url, headers=headers, timeout=20)
        _track_weather_request()
        forecast_response.raise_for_status()
        forecast_data = forecast_response.json()

        periods = forecast_data.get("properties", {}).get("periods", [])
        forecast_text = "\n".join(
            f"{p.get('name')}: {p.get('detailedForecast')}"
            for p in periods[:6]
        )

        return WeatherForecast(
            station_code=station.code,
            station_name=station.name,
            source="weather_gov",
            forecast_text=forecast_text,
            raw=forecast_data,
        )

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None

        if status == 404:
            return _safe_open_meteo(station)

        raise

    except requests.RequestException:
        return _safe_open_meteo(station)


def _safe_open_meteo(station: WeatherStation) -> WeatherForecast | None:
    try:
        return _fetch_open_meteo_forecast(station)
    except Exception:
        return None
