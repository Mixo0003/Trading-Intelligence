"""Safe read-only live evidence check for PoC stations. No secrets printed."""
from __future__ import annotations

import json
from datetime import date, timedelta

from app.config.settings import load_settings
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.evidence.local_tv import fetch_local_tv_evidence
from app.weather.evidence.metar import fetch_metar_evidence
from app.weather.evidence.nws_discussion import fetch_nws_discussion
from app.weather.evidence.wunderground_history import fetch_wunderground_history_evidence
from app.weather.sources.nws import fetch_nws_hourly
from app.weather.station_resolver import resolve_verified_station_from_text
from app.weather.stations import get_station

STATIONS = ("KORD", "KATL", "KSEA", "KMIA", "KDAL")


def main() -> None:
    settings = load_settings()
    target = date.today() - timedelta(days=1)
    out = {}
    for code in STATIONS:
        station = get_station(code)
        if station is None or not station.verified:
            resolved, err = resolve_verified_station_from_text(
                f"{code} airport https://www.wunderground.com/history/daily/{code}",
                settings,
            )
            station = resolved
            if station is None:
                out[code] = {"error": err or "unresolved"}
                continue
        # Ensure verified flag for NWS path
        if not station.verified:
            station = type(station)(
                code=station.code,
                name=station.name,
                latitude=station.latitude,
                longitude=station.longitude,
                elevation_m=station.elevation_m,
                timezone=station.timezone or "UTC",
                verified=True,
                resolution_url=station.resolution_url,
            )

        wu = fetch_wunderground_history_evidence(station, target=target)
        metar = fetch_metar_evidence(station)
        nws_pt = fetch_nws_hourly(
            settings,
            station,
            date.today(),
            NormalizedTemperatureType.DAILY_HIGH,
            station.timezone or "UTC",
        )
        disc = fetch_nws_discussion(settings, station)
        tv = fetch_local_tv_evidence(code)
        out[code] = {
            "wunderground_history": {
                "ok": bool(wu.recent_days),
                "url_present": bool(wu.history_url),
                "days": len(wu.recent_days),
                "observed_source": wu.observed_source,
                "error": wu.error,
            },
            "metar": {
                "ok": metar.temperature_c is not None and not metar.stale,
                "stale": metar.stale,
                "error": metar.error,
            },
            "nws_point_forecast": {"ok": nws_pt is not None},
            "nws_discussion": {
                "ok": disc is not None and disc.error is None and bool(disc.office_id),
                "office": disc.office_id if disc else None,
                "error": disc.error if disc else None,
            },
            "local_tv": [
                {
                    "source": item.source_name,
                    "accepted": item.accepted,
                    "reject": item.reject_reason,
                    "has_high": item.high_f is not None,
                }
                for item in tv
            ],
        }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
