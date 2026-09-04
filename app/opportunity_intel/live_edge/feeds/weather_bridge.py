"""Reuse existing Weather Intelligence METAR as Live Edge input. No second forecast engine."""

from __future__ import annotations

from datetime import datetime, timezone

from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)
from app.weather.resolution import extract_metric, extract_resolution_source, extract_station_code
from app.weather.stations import get_station


class WeatherIntelligenceBridge:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        if identity.category != LiveEdgeCategory.WEATHER:
            return ()
        text = " ".join(
            part
            for part in (market.question, market.event_title, market.description)
            if part
        )
        station_code = extract_station_code(text)
        source = extract_resolution_source(text)
        metric = extract_metric(text)
        if not station_code:
            return ()
        extra = {
            "outcome_confirmed": False,
            "unresolved_conditions": [
                "weather day not confirmed complete by primary source"
            ],
            "station": station_code,
            "resolution_source": source,
            "metric": metric,
        }
        source_url = f"https://api.weather.gov/stations/{station_code}/observations/latest"
        station = get_station(station_code)
        if station is not None:
            try:
                from app.weather.evidence.metar import fetch_metar_evidence

                evidence = fetch_metar_evidence(station)
                extra["metar_temp_c"] = getattr(evidence, "temperature_c", None)
                source_url = getattr(evidence, "source_url", None) or source_url
            except Exception:
                extra["unresolved_conditions"] = [
                    "weather METAR fetch failed; no invented observation"
                ]
        return (
            LiveStateSnapshot(
                sport="Weather",
                status="observed",
                participants=(station_code,),
                extra=extra,
                provenance=LiveDataProvenance(
                    provider="nws-metar",
                    event_identifier=station_code,
                    source_locator=source_url,
                    fetched_at=now,
                    source_timestamp=now,
                    reliability="primary_resolution",
                ),
            ),
        )
