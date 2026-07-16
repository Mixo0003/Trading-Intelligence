"""Read-only live check for MET / ECMWF IFS / DWD ICON / NWS at sample stations."""

from __future__ import annotations

import json
from datetime import date, timedelta

from app.config.settings import load_settings
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.forecast_ensemble import fetch_forecast_ensemble
from app.weather.sources.met_norway import fetch_met_norway
from app.weather.sources.nws import fetch_nws_hourly
from app.weather.sources.open_meteo_models import (
    clear_model_caches,
    fetch_dwd_icon_global,
    fetch_ecmwf_ifs,
)
from app.weather.station_resolver import resolve_verified_station_from_text
from app.weather.temperature_units import TemperatureUnit


STATIONS = (
    ("Europe", "EGLL", "Europe/London"),
    ("Asia", "RJTT", "Asia/Tokyo"),
    ("USA", "KORD", "America/Chicago"),
)


def main() -> None:
    clear_model_caches()
    settings = load_settings()
    target = date.today() + timedelta(days=1)
    out = {}
    for region, code, tz in STATIONS:
        station, err = resolve_verified_station_from_text(
            f"{code} https://www.wunderground.com/history/daily/{code}",
            settings,
        )
        if station is None:
            out[code] = {"error": err or "unresolved"}
            continue
        if not station.timezone:
            station = type(station)(
                code=station.code,
                name=station.name,
                latitude=station.latitude,
                longitude=station.longitude,
                elevation_m=station.elevation_m,
                timezone=tz,
                verified=True,
                resolution_url=station.resolution_url,
            )
        event = NormalizedTemperatureType.DAILY_HIGH
        met = fetch_met_norway(station, target, event, station.timezone or tz)
        ecmwf = fetch_ecmwf_ifs(station, target, event, station.timezone or tz)
        icon = fetch_dwd_icon_global(station, target, event, station.timezone or tz)
        nws = None
        if code.startswith("K"):
            nws = fetch_nws_hourly(
                settings, station, target, event, station.timezone or tz
            )
        ensemble = fetch_forecast_ensemble(
            settings,
            station,
            target.isoformat(),
            event,
            market_unit=TemperatureUnit.FAHRENHEIT
            if code.startswith("K")
            else TemperatureUnit.CELSIUS,
        )
        out[code] = {
            "region": region,
            "station": station.code,
            "lat": station.latitude,
            "lon": station.longitude,
            "timezone": station.timezone,
            "target_local_date": target.isoformat(),
            "event": "daily_high",
            "met_norway_c": None if met is None else met.selected_value_c,
            "ecmwf_ifs": None
            if ecmwf is None
            else {
                "c": ecmwf.selected_value_c,
                "model": ecmwf.model_name,
                "provider": ecmwf.provider_name,
                "hours": len(ecmwf.hourly_samples),
            },
            "dwd_icon": None
            if icon is None
            else {
                "c": icon.selected_value_c,
                "model": icon.model_name,
                "provider": icon.provider_name,
                "hours": len(icon.hourly_samples),
            },
            "nws_c": None if nws is None else nws.selected_value_c,
            "ensemble_providers": list(ensemble.provider_names) if ensemble else [],
            "ensemble_count": len(ensemble.provider_names) if ensemble else 0,
            "betting_skip": ensemble.betting_skip_reason if ensemble else None,
            "identity_ok": bool(
                ecmwf
                and icon
                and ecmwf.provider_name == "ECMWF IFS"
                and icon.provider_name == "DWD ICON Global"
                and ecmwf.model_name == "ecmwf_ifs025"
                and icon.model_name == "icon_global"
                and ecmwf.selected_value_c != icon.selected_value_c
            ),
        }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
