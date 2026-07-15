#!/usr/bin/env python3
"""Evidence-based audit for Paris temperature market — read-only, no secrets."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings
from app.polymarket.client import PolymarketClient
from app.polymarket.market_text import build_market_context_text
from app.polymarket.market_type import (
    classify_temperature_market,
    normalize_temperature_type,
    normalized_type_label,
)
from app.polymarket.markets import get_event_by_slug, get_weather_markets
from app.polymarket.parser import parse_market
from app.polymarket.pricing import extract_yes_price, market_polymarket_url
from app.signals.market_analysis import analyze_market
from app.signals.outcome_labels import build_best_outcome_label, extract_exact_temperature
from app.weather.forecast import fetch_weather_gov_forecast
from app.weather.resolution import parse_resolution_details
from app.weather.station_resolver import resolve_station_from_text
from app.weather.temperature_extract import extract_forecast_temperature_f
from app.weather.temperature_units import TemperatureUnit, detect_market_unit, provider_unit_for_forecast
from app.signals.temperature_analysis import forecast_in_market_unit


def _find_paris_event(client: PolymarketClient):
    slug_candidates = [
        "highest-temperature-in-paris-on-july-16-2026",
        "highest-temperature-in-paris-on-july-16",
    ]
    for slug in slug_candidates:
        event = get_event_by_slug(client, slug)
        if event:
            return event

    markets, _, _ = get_weather_markets(client, limit=100)
    for market in markets:
        text = build_market_context_text(market).lower()
        if "paris" in text and "highest temperature" in text:
            events = market.raw.get("events") or []
            if events:
                from app.polymarket.parser import parse_event
                return parse_event(events[0])
    return None


def _trace_child(market, settings):
    text = build_market_context_text(market)
    resolution = parse_resolution_details(text)
    market_type, direction = classify_temperature_market(text)
    normalized = normalize_temperature_type(text)
    unit, unit_source = detect_market_unit(market, context_text=text)
    station = resolve_station_from_text(text)

    forecast = None
    forecast_f = None
    daily_min_c = None
    daily_max_c = None
    provider = None

    if station:
        forecast = fetch_weather_gov_forecast(settings, station)
        if forecast:
            provider = forecast.source
            forecast_f = extract_forecast_temperature_f(
                forecast=forecast,
                target_date_text=resolution.target_date,
                market_type=market_type,
                direction=direction,
            )
            if forecast.source == "open_meteo":
                daily = forecast.raw.get("daily", {})
                dates = daily.get("time", [])
                maxes = daily.get("temperature_2m_max", [])
                mins = daily.get("temperature_2m_min", [])
                for i, d in enumerate(dates):
                    if "2026-07-16" in d or "july 16" in (resolution.target_date or "").lower():
                        if i < len(maxes):
                            daily_max_c = maxes[i]
                        if i < len(mins):
                            daily_min_c = mins[i]

    forecast_value = None
    if forecast_f is not None:
        pu = provider_unit_for_forecast(forecast.source if forecast else "open_meteo")
        forecast_value, _ = forecast_in_market_unit(forecast_f, pu, unit)

    best_outcome = build_best_outcome_label(
        market,
        normalized,
        unit,
        context_text=text,
        threshold_display=resolution.threshold_f,
    )
    exact = extract_exact_temperature(text)
    analysis = analyze_market(settings, market)

    return {
        "child_market_id": market.id,
        "child_slug": market.slug,
        "child_question": market.question,
        "groupItemTitle": market.raw.get("groupItemTitle"),
        "outcomes": market.outcomes,
        "outcome_prices": market.outcome_prices,
        "yes_price": extract_yes_price(market),
        "parsed_market_type_enum": market_type.value,
        "parsed_direction": direction.value,
        "normalized_type": normalized.value,
        "normalized_type_label": normalized_type_label(normalized),
        "threshold_f": resolution.threshold_f,
        "extract_exact_temperature": exact,
        "target_date": resolution.target_date,
        "station_code": resolution.station_code,
        "resolution_source": resolution.source,
        "weather_provider": provider,
        "forecast_f_from_extractor": forecast_f,
        "forecast_display_value": forecast_value,
        "open_meteo_daily_max": daily_max_c,
        "open_meteo_daily_min": daily_min_c,
        "best_outcome_label": best_outcome,
        "analysis_temperature_type": analysis.temperature_type_label,
        "analysis_forecast": analysis.forecast_display,
        "analysis_best_outcome": analysis.best_outcome,
        "analysis_model_prob": analysis.model_probability,
        "analysis_market_price": analysis.raw_yes_price,
        "analysis_edge": analysis.edge,
        "analysis_signal": analysis.recommendation_tier,
        "code_path_notes": {
            "market_type_from": "classify_temperature_market(build_market_context_text)",
            "normalized_type_from": "normalize_temperature_market(build_market_context_text)",
            "forecast_from": "extract_forecast_temperature_f(market_type, direction)",
            "best_outcome_from": "build_best_outcome_label(groupItemTitle/question)",
            "uses_min_when": "direction==BELOW or market_type==MINIMUM_TEMPERATURE",
            "uses_max_when": "direction==ABOVE or market_type in MAXIMUM/ABOVE_THRESHOLD",
        },
    }


def main():
    settings = load_settings()
    client = PolymarketClient(settings)
    event = _find_paris_event(client)

    if event is None:
        print(json.dumps({"error": "Paris event not found in live scan"}, indent=2))
        return

    children = []
    for raw in event.raw.get("markets", []) or []:
        if isinstance(raw, dict):
            children.append(parse_market(raw))

    report = {
        "event_id": event.id,
        "event_title": event.title,
        "event_slug": event.slug,
        "event_description_excerpt": (event.description or "")[:300],
        "child_count": len(children),
        "children_summary": [
            {
                "id": m.id,
                "question": m.question,
                "groupItemTitle": m.raw.get("groupItemTitle"),
                "outcomes": m.outcomes,
                "outcome_prices": m.outcome_prices,
                "slug": m.slug,
            }
            for m in children
        ],
        "child_traces": [_trace_child(m, settings) for m in children[:12]],
    }

    # Find the child that produced 24°C or lowest forecast
    suspicious = [
        t for t in report["child_traces"]
        if t.get("analysis_best_outcome") and "24" in str(t.get("analysis_best_outcome"))
        or t.get("analysis_forecast") and "19" in str(t.get("analysis_forecast"))
    ]
    report["suspicious_children"] = suspicious

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
