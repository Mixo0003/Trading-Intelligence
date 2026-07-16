"""Station-first weather evidence (observations + qualitative). Not forecast providers."""

from app.weather.evidence.station_evidence import (
    gather_station_evidence,
    qualitative_provider_count,
)
from app.weather.evidence.types import StationEvidence
from app.weather.evidence.wunderground_history import (
    build_wunderground_history_url,
    map_observed_to_band,
    polymarket_round_temp_f,
)

__all__ = [
    "StationEvidence",
    "gather_station_evidence",
    "qualitative_provider_count",
    "build_wunderground_history_url",
    "map_observed_to_band",
    "polymarket_round_temp_f",
]
