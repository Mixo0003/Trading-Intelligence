#!/usr/bin/env python3
"""Live read-only Paris event verification."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings
from app.polymarket.client import PolymarketClient
from app.polymarket.markets import get_event_by_slug
from app.polymarket.parser import parse_market
from app.polymarket.sibling_outcomes import outcome_labels, parse_sibling_outcomes
from app.signals.bucket_probability import compute_sibling_probabilities
from app.signals.event_analysis import analyze_temperature_event
from app.telegram.formatting import format_concise_opportunity
from app.weather.daily_forecast import fetch_daily_forecast_stats
from app.polymarket.event_type import detect_event_temperature_type
from app.polymarket.market_text import build_market_context_text
from app.weather.resolution import parse_resolution_details
from app.weather.station_resolver import resolve_station_from_text


def main() -> None:
    settings = load_settings()
    client = PolymarketClient(settings)
    event = get_event_by_slug(client, "highest-temperature-in-paris-on-july-16-2026")
    if not event:
        print(json.dumps({"error": "Paris event not found"}))
        return

    children = [parse_market(m.raw) if hasattr(m, "raw") else m for m in event.markets]
    analysis = analyze_temperature_event(settings, children)

    context = build_market_context_text(children[0])
    resolution = parse_resolution_details(context)
    station = resolve_station_from_text(context)
    event_type = detect_event_temperature_type(event.title, event.description)
    stats = None
    if station:
        stats = fetch_daily_forecast_stats(station, resolution.target_date, event_type)

    siblings = parse_sibling_outcomes(children)
    probs = compute_sibling_probabilities(siblings, stats.selected_value_c) if stats else []

    report = {
        "event_title": event.title,
        "market_type": analysis.temperature_type.value if analysis else None,
        "market_type_label": analysis.temperature_type_label if analysis else None,
        "target_date": resolution.target_date,
        "station": resolution.station_code,
        "timezone": stats.station_timezone if stats else None,
        "provider": stats.provider if stats else None,
        "hourly_min_c": stats.daily_min_c if stats else None,
        "hourly_max_c": stats.daily_max_c if stats else None,
        "selected_statistic": stats.selected_statistic if stats else None,
        "selected_forecast_c": stats.selected_value_c if stats else None,
        "active_outcomes": sorted(outcome_labels(siblings)),
        "outcome_probabilities": {
            item.outcome.label: round(item.probability, 4) for item in probs
        },
        "prob_sum": round(sum(item.probability for item in probs), 4) if probs else None,
        "selected_outcome": analysis.best_outcome if analysis else None,
        "selected_market_price": analysis.raw_yes_price if analysis else None,
        "model_probability": analysis.model_probability if analysis else None,
        "edge": analysis.edge if analysis else None,
        "confidence": analysis.confidence if analysis else None,
        "signal": analysis.recommendation_tier if analysis else None,
        "validation_error": analysis.validation_error if analysis else None,
        "telegram_report": format_concise_opportunity(analysis) if analysis else None,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
