"""Live read-only verification for Miami lowest-temperature event via scan path."""

from __future__ import annotations

import json
import sys

from app.config.settings import load_settings
from app.polymarket.client import PolymarketClient
from app.polymarket.event_expansion import expand_group_children
from app.polymarket.markets import get_event_by_slug
from app.polymarket.sibling_outcomes import parse_sibling_outcomes
from app.signals.bucket_probability import compute_sibling_probabilities, default_sigma
from app.signals.event_analysis import analyze_temperature_event
from app.signals.scan_service import scan_weather_markets
from app.telegram.formatting import format_concise_opportunity
from app.weather.forecast_ensemble import fetch_forecast_ensemble
from app.weather.station_resolver import resolve_verified_station_from_text
from app.weather.temperature_units import TemperatureUnit, convert_temperature, unit_symbol
from app.polymarket.market_text import build_market_context_text
from app.polymarket.event_type import detect_event_temperature_type
from app.weather.resolution import parse_resolution_details


def main() -> int:
    settings = load_settings()
    slug = "lowest-temperature-in-miami-on-july-15-2026"
    client = PolymarketClient(settings)
    event = get_event_by_slug(client, slug)
    if not event or not event.markets:
        print(json.dumps({"error": "event_not_found", "slug": slug}))
        return 1

    children = expand_group_children(client, event.markets)
    context = build_market_context_text(children[0])
    resolution = parse_resolution_details(context)
    event_type = detect_event_temperature_type(event.title, event.description)
    station, station_error = resolve_verified_station_from_text(context, settings)

    ensemble = None
    if station and station.verified:
        ensemble = fetch_forecast_ensemble(
            settings,
            station,
            resolution.target_date,
            event_type,
            market_unit=TemperatureUnit.FAHRENHEIT,
        )

    analysis = analyze_temperature_event(settings, children)
    siblings = parse_sibling_outcomes(children)
    forecast_native = None
    probs = []
    if ensemble and not ensemble.skip_reason:
        forecast_native = convert_temperature(
            ensemble.ensemble_value_c,
            TemperatureUnit.CELSIUS,
            TemperatureUnit.FAHRENHEIT,
        )
        probs = compute_sibling_probabilities(
            siblings,
            forecast_native,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=default_sigma(TemperatureUnit.FAHRENHEIT),
        )

    scan = scan_weather_markets(settings, limit=100)
    scan_match = next(
        (
            item for item in scan.results
            if "Lowest temperature in Miami" in (item.market_title or "")
        ),
        None,
    )

    report = {
        "event_title": event.title,
        "target_date": resolution.target_date,
        "market_type": event_type.value if event_type else None,
        "resolution_url": station.resolution_url if station else resolution.source,
        "resolution_station": station.code if station else None,
        "station_error": station_error,
        "station_coordinates": {
            "latitude": station.latitude,
            "longitude": station.longitude,
        } if station else None,
        "station_elevation_m": station.elevation_m if station else None,
        "timezone": station.timezone if station else None,
        "providers": [
            {
                "name": provider.provider_name,
                "model": provider.model_name,
                "grid_latitude": provider.grid_latitude,
                "grid_longitude": provider.grid_longitude,
                "grid_distance_km": provider.grid_distance_km,
                "selected_f": round(
                    provider.selected_value_c * 9 / 5 + 32,
                    1,
                ),
                "usable": provider.usable,
                "reject_reason": provider.reject_reason,
            }
            for provider in (ensemble.providers if ensemble else [])
        ],
        "ensemble_forecast_low_f": round(
            ensemble.ensemble_value_c * 9 / 5 + 32,
            1,
        ) if ensemble else None,
        "provider_spread_f": round(
            ensemble.provider_spread_c * 9 / 5,
            1,
        ) if ensemble else None,
        "ensemble_skip_reason": ensemble.skip_reason if ensemble else None,
        "sibling_outcomes": [
            {
                "market_id": item.market_id,
                "label": item.label,
                "yes_price": item.yes_price,
                "model_probability": round(
                    next(
                        (
                            prob.probability
                            for prob in probs
                            if prob.outcome.market_id == item.market_id
                        ),
                        0.0,
                    ),
                    3,
                ),
            }
            for item in siblings
        ],
        "analysis": {
            "forecast_display": analysis.forecast_display if analysis else None,
            "best_outcome": analysis.best_outcome if analysis else None,
            "edge": analysis.edge if analysis else None,
            "signal": analysis.recommendation_tier if analysis else None,
            "validation_error": analysis.validation_error if analysis else None,
            "forecast_providers": analysis.forecast_providers if analysis else None,
            "provider_spread": analysis.provider_spread_display if analysis else None,
        },
        "scan_path_match": {
            "found": scan_match is not None,
            "forecast_display": scan_match.forecast_display if scan_match else None,
            "best_outcome": scan_match.best_outcome if scan_match else None,
            "signal": scan_match.recommendation_tier if scan_match else None,
        },
        "telegram_block": format_concise_opportunity(analysis) if analysis else None,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
