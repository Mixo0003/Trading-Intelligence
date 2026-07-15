#!/usr/bin/env python3
"""Evidence-based audit for Miami temperature bracket event — read-only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings
from app.ev import determine_recommendation_tier
from app.ev.types import ConfidenceLevel, MarketCategory
from app.polymarket.client import PolymarketClient
from app.polymarket.event_type import detect_event_temperature_type
from app.polymarket.market_text import build_market_context_text
from app.polymarket.markets import get_event_by_slug
from app.polymarket.parser import parse_market
from app.polymarket.pricing import extract_yes_price
from app.polymarket.sibling_outcomes import parse_sibling_outcomes
from app.signals.bucket_probability import compute_sibling_probabilities, default_sigma
from app.signals.event_analysis import analyze_temperature_event
from app.signals.outcome_labels import extract_bracket_bounds
from app.weather.daily_forecast import fetch_daily_forecast_stats
from app.weather.resolution import parse_resolution_details
from app.weather.station_resolver import resolve_station_from_text
from app.weather.temperature_units import TemperatureUnit, celsius_to_fahrenheit, convert_temperature, detect_market_unit


def main() -> None:
    settings = load_settings()
    client = PolymarketClient(settings)
    slug_candidates = [
        "highest-temperature-in-miami-on-july-15-2026",
        "highest-temperature-in-miami-on-july-15",
    ]
    event = None
    for slug in slug_candidates:
        event = get_event_by_slug(client, slug)
        if event:
            break
    if not event:
        print(json.dumps({"error": "Miami event not found"}))
        return

    children = [parse_market(m.raw) if hasattr(m, "raw") else m for m in event.markets]
    context = build_market_context_text(children[0])
    resolution = parse_resolution_details(context)
    station = resolve_station_from_text(context)
    event_type = detect_event_temperature_type(event.title, event.description)
    market_unit, _ = detect_market_unit(children[0], context_text=context)

    stats = None
    if station:
        stats = fetch_daily_forecast_stats(station, resolution.target_date, event_type)

    siblings = parse_sibling_outcomes(children)
    forecast_c = stats.selected_value_c if stats else None
    forecast_native = (
        convert_temperature(forecast_c, TemperatureUnit.CELSIUS, market_unit)
        if forecast_c is not None
        else None
    )
    sigma_native = default_sigma(market_unit)
    probs = (
        compute_sibling_probabilities(
            siblings,
            forecast_native,
            unit=market_unit,
            sigma=sigma_native,
        )
        if forecast_native is not None
        else []
    )
    analysis = analyze_temperature_event(settings, children)

    prob_by_id = {p.outcome.market_id: p.probability for p in probs}
    selected_id = analysis.market_id if analysis else None

    rows = []
    for child in children:
        raw = child.raw
        label = str(raw.get("groupItemTitle") or child.question or "")
        parsed = next((s for s in siblings if s.market_id == child.id), None)
        prob = prob_by_id.get(child.id)
        yes_price = extract_yes_price(child)
        no_price = None
        prices = child.outcome_prices
        if isinstance(prices, list) and len(prices) > 1:
            try:
                no_price = float(prices[1])
            except (TypeError, ValueError):
                pass
        bracket = extract_bracket_bounds(label, market_unit)
        edge = ev = None
        if prob is not None and yes_price is not None:
            result = determine_recommendation_tier(
                prob, yes_price, ConfidenceLevel.HIGH, market_category=MarketCategory.WEATHER
            )
            edge = result.edge
            ev = result.ev
        rows.append({
            "child_market_id": child.id,
            "raw_question": child.question,
            "groupItemTitle": raw.get("groupItemTitle"),
            "raw_outcomes": child.outcomes,
            "raw_prices": child.outcome_prices,
            "normalized_label": parsed.label if parsed else None,
            "parsed_kind": parsed.kind.value if parsed else None,
            "exact_value": parsed.exact_value if parsed else None,
            "lower_bound": parsed.lower_bound if parsed else None,
            "upper_bound": parsed.upper_bound if parsed else None,
            "bracket_bounds_from_labels": bracket,
            "live_yes_price": yes_price,
            "live_no_price": no_price,
            "active": raw.get("active"),
            "model_probability": prob,
            "edge": edge,
            "ev": ev,
            "selected": child.id == selected_id,
        })

    report = {
        "event_id": event.id,
        "event_title": event.title,
        "event_slug": event.slug,
        "target_date": resolution.target_date,
        "station": resolution.station_code,
        "timezone": stats.station_timezone if stats else None,
        "provider": stats.provider if stats else None,
        "market_unit": market_unit.value,
        "hourly_samples": stats.hourly_samples if stats else None,
        "daily_max_c": stats.daily_max_c if stats else None,
        "daily_max_f": celsius_to_fahrenheit(stats.daily_max_c) if stats else None,
        "forecast_center_c": forecast_c,
        "forecast_center_f": celsius_to_fahrenheit(forecast_c) if forecast_c else None,
        "forecast_native": forecast_native,
        "sigma_f": sigma_native if market_unit == TemperatureUnit.FAHRENHEIT else None,
        "sigma_c": default_sigma(TemperatureUnit.CELSIUS),
        "probability_sum": sum(p.probability for p in probs) if probs else None,
        "sibling_table": rows,
        "analysis": {
            "best_outcome": analysis.best_outcome if analysis else None,
            "selected_market_id": analysis.market_id if analysis else None,
            "model_probability": analysis.model_probability if analysis else None,
            "market_price": analysis.raw_yes_price if analysis else None,
            "edge": analysis.edge if analysis else None,
            "signal": analysis.recommendation_tier if analysis else None,
            "validation_error": analysis.validation_error if analysis else None,
        },
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
