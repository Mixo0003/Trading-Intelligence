"""Live Edge defaults. UI exposes only the ON/OFF toggle; numbers stay here."""

from __future__ import annotations

from decimal import Decimal

from app.opportunity_intel.settings import BALANCED_MAXIMUM_BOOK_AGE_SECONDS

DEFAULT_LIVE_EDGE_ENABLED = False

MINIMUM_CONSERVATIVE_EDGE_PP = Decimal("0.05")
PREFERRED_CONSERVATIVE_EDGE_PP = Decimal("0.08")
MINIMUM_EXPECTED_ROI = Decimal("0.03")
PREFERRED_EXPECTED_ROI = Decimal("0.05")

NEAR_RESOLUTION_DISCOVERY_PROBABILITY = Decimal("0.85")
NEAR_RESOLUTION_MINIMUM_EDGE_PP = Decimal("0.03")
NEAR_RESOLUTION_PREFERRED_EDGE_PP = Decimal("0.05")

# Quote freshness: stricter than balanced 30s last-price usage — book age bound.
QUOTE_MAX_AGE_SECONDS = min(15, int(BALANCED_MAXIMUM_BOOK_AGE_SECONDS))
LIVE_STATE_MAX_AGE_SECONDS = 30
WEATHER_EVIDENCE_MAX_AGE_SECONDS = 3600
CALCULATION_MAX_AGE_SECONDS = 60

# Provider-specific live-state freshness (fetch + documented source delay).
LIVE_STATE_MAX_AGE_BY_PROVIDER: dict[str, int] = {
    "mlb-statsapi": 30,
    "opendota-live": 150,
    "thesportsdb-livescore": 90,
    "openligadb": 90,
    "liquipedia": 120,
    "leaguepedia-lol": 180,
    "bo3-cs2": 30,
    "lolesports": 30,
    "pandascore": 30,
    "weather-intelligence": WEATHER_EVIDENCE_MAX_AGE_SECONDS,
    "nws-metar": 600,
    "binance-spot": 30,
    "fixture-live-state": LIVE_STATE_MAX_AGE_SECONDS,
}

NEAR_RESOLUTION_MAX_SECONDS = 6 * 3600
LIVE_MAP_NEAR_END_SECONDS = 1800

TOO_LATE_PROBABILITY_FLOOR = Decimal("0.95")
TOO_LATE_PRICE_FLOOR = Decimal("0.97")
TOO_LATE_EDGE_CEILING_PP = Decimal("0.02")

PROBABILITY_DISPLAY_QUANTUM = Decimal("0.01")
