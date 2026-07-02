from dataclasses import dataclass
from typing import Any

import requests

from app.config.settings import Settings
from app.weather.stations import WeatherStation


@dataclass(frozen=True)
class WeatherForecast:
    station_code: str
    station_name: str
    source: str
    forecast_text: str
    raw: dict[str, Any]


def fetch_weather_gov_forecast(
    settings: Settings,
    station: WeatherStation,
) -> WeatherForecast:
    headers = {
        "User-Agent": "Trading Intelligence Bot contact@example.com",
        "Accept": "application/geo+json, application/json",
    }

    points_url = f"{settings.weather_gov_base_url}/points/{station.latitude},{station.longitude}"
    points_response = requests.get(points_url, headers=headers, timeout=20)
    points_response.raise_for_status()
    points_data = points_response.json()

    forecast_url = points_data["properties"]["forecast"]
    forecast_response = requests.get(forecast_url, headers=headers, timeout=20)
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
