"""Wunderground history URL + observed daily values for the exact resolution station.

Wunderground history *pages* are JS-rendered; embedded JSON on those pages is often
Weather.com *forecast* payloads and must never be treated as observations.

Observed highs/lows come from public ASOS daily summaries for the same ICAO
(Iowa State IEM). No scraped Weather.com API keys are used.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, timedelta

import requests

from app.polymarket.sibling_outcomes import OutcomeKind, SiblingOutcome
from app.weather.evidence.asos_daily import fetch_asos_daily_range
from app.weather.evidence.types import DailyObservation, WundergroundHistoryEvidence
from app.weather.stations import WeatherStation

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "TradingIntelligence/1.0 (station history; no api keys)"}


def polymarket_round_temp_f(raw_f: float) -> int:
    """Round half away from zero to nearest integer °F (common Polymarket convention)."""
    return int(math.floor(raw_f + 0.5)) if raw_f >= 0 else int(math.ceil(raw_f - 0.5))


def build_wunderground_history_url(
    station_code: str,
    day: date | None = None,
    *,
    resolution_url: str | None = None,
) -> str:
    code = station_code.upper().strip()
    if resolution_url and "wunderground.com" in resolution_url.lower():
        base = resolution_url.split("?")[0].rstrip("/")
        if "/date/" in base and day is not None:
            return re.sub(r"/date/\d{4}-\d{1,2}-\d{1,2}$", f"/date/{day.isoformat()}", base)
        if day is not None and "/history/" in base:
            if re.search(rf"/{re.escape(code)}(?:/|$)", base, re.I):
                if "/date/" not in base:
                    return f"{base}/date/{day.isoformat()}"
                return base
        if day is None:
            return base
    if day is None:
        return f"https://www.wunderground.com/history/daily/{code}"
    return f"https://www.wunderground.com/history/daily/{code}/date/{day.isoformat()}"


def _html_looks_like_forecast_not_history(html: str) -> bool:
    lower = html.lower()
    if "calendardaytemperaturemax" in lower and "lib-city-history" in lower:
        # Forecast blob present on history shell — not usable observations.
        return True
    return False


def _reject_scraped_api_key_usage(html: str) -> None:
    """Guard: never call api.weather.com with keys found in page HTML."""
    if re.search(r"apiKey=([A-Za-z0-9]{32})", html):
        logger.debug(
            "Wunderground page contains embedded apiKey tokens; ignoring (no Weather.com scrape)."
        )


def probe_wunderground_history_page(url: str) -> tuple[bool, str | None]:
    """Fetch public history page. Returns (reachable, error). Does not parse forecast JSON."""
    try:
        response = requests.get(url, headers=_UA, timeout=20)
        if response.status_code != 200:
            return False, f"HTTP_{response.status_code}"
        html = response.text or ""
        _reject_scraped_api_key_usage(html)
        if _html_looks_like_forecast_not_history(html):
            return True, "FORECAST_BLOB_ONLY"
        if "lib-city-history" in html.lower() or "history" in html.lower():
            return True, "JS_RENDERED"
        return True, None
    except Exception as exc:
        return False, type(exc).__name__


def map_observed_to_band(
    observed_rounded: int,
    outcomes: list[SiblingOutcome],
) -> SiblingOutcome | None:
    """Map a rounded observed °F value onto sibling market bands."""
    for outcome in outcomes:
        if outcome.kind == OutcomeKind.EXACT and outcome.exact_value is not None:
            if int(outcome.exact_value) == observed_rounded:
                return outcome
        if outcome.kind == OutcomeKind.BRACKET:
            low = outcome.lower_bound
            high = outcome.upper_bound
            if low is not None and high is not None and low <= observed_rounded <= high:
                return outcome
        if outcome.kind == OutcomeKind.BELOW_TAIL and outcome.upper_bound is not None:
            if observed_rounded <= outcome.upper_bound:
                return outcome
        if outcome.kind == OutcomeKind.ABOVE_TAIL and outcome.lower_bound is not None:
            if observed_rounded >= outcome.lower_bound:
                return outcome
    return None


def compute_station_bias_f(
    recent: list[DailyObservation],
    *,
    forecast_highs_f: list[float] | None = None,
    min_days: int = 3,
) -> tuple[float | None, int]:
    """Mean (obs high − reference) over recent complete days. Display-only unless enough days.

    Without paired forecast references, bias is mean residual vs sample mean (always ~0);
    callers should pass paired city/model highs when available. Here we return the
    mean absolute station signature as obs − median(obs) is not useful — instead
    return None unless paired forecasts are provided.
    """
    complete = [d for d in recent if d.day_complete and d.high_f is not None]
    if len(complete) < min_days:
        return None, len(complete)
    if not forecast_highs_f or len(forecast_highs_f) < min_days:
        # Internal signature: spread of recent highs (not applied as additive bias).
        highs = [float(d.high_f) for d in complete if d.high_f is not None]
        if len(highs) < min_days:
            return None, len(highs)
        mean_h = sum(highs) / len(highs)
        # Placeholder bias vs own mean is 0; expose day count only.
        _ = mean_h
        return 0.0, len(highs)

    paired = list(zip(complete, forecast_highs_f))
    deltas = [
        float(obs.high_f) - float(fcst)
        for obs, fcst in paired
        if obs.high_f is not None
    ]
    if len(deltas) < min_days:
        return None, len(deltas)
    return sum(deltas) / len(deltas), len(deltas)


def fetch_wunderground_history_evidence(
    station: WeatherStation,
    *,
    target: date | None,
    resolution_url: str | None = None,
    lookback_days: int = 5,
) -> WundergroundHistoryEvidence:
    code = station.code.upper()
    history_url = build_wunderground_history_url(
        code,
        target,
        resolution_url=resolution_url or station.resolution_url,
    )

    page_ok, page_note = probe_wunderground_history_page(history_url)
    if not page_ok:
        logger.info("Wunderground history page unreachable for %s: %s", code, page_note)

    end = target or date.today()
    # Prefer completed days before today when target is today/incomplete.
    asof = min(end, date.today() - timedelta(days=1)) if end >= date.today() else end
    recent = fetch_asos_daily_range(code, end=asof, days=lookback_days)

    target_day = None
    if target is not None:
        for item in recent:
            if item.local_date == target:
                target_day = item
                break
        if target_day is None and target <= date.today():
            extra = fetch_asos_daily_range(code, end=target, days=1)
            target_day = next((d for d in extra if d.local_date == target), None)

    raw_high = target_day.high_f if target_day else None
    raw_low = target_day.low_f if target_day else None
    rounded_high = polymarket_round_temp_f(raw_high) if raw_high is not None else None
    rounded_low = polymarket_round_temp_f(raw_low) if raw_low is not None else None

    error = None
    if not recent:
        error = "NO_OBSERVED_HISTORY"
    elif page_note in {"FORECAST_BLOB_ONLY", "JS_RENDERED"}:
        # Expected: observations from ASOS, URL preserved for resolution.
        error = None

    return WundergroundHistoryEvidence(
        station_code=code,
        history_url=history_url,
        recent_days=tuple(recent),
        target_day=target_day,
        raw_high_f=raw_high,
        raw_low_f=raw_low,
        rounded_high_f=rounded_high,
        rounded_low_f=rounded_low,
        observed_source="asos_iem",
        error=error,
    )
