#!/usr/bin/env python3
"""Read-only live verification scan (no Telegram polling)."""

from __future__ import annotations

import json
from pathlib import Path

from app.config.settings import load_settings
from app.polymarket.models import PolymarketMarket
from app.polymarket.parser import parse_market
from app.signals.market_analysis import analyze_market
from app.signals.scan_service import scan_weather_markets
from app.telegram.buttons import main_menu_keyboard, weather_menu_keyboard
from app.telegram.formatting import format_concise_opportunity
from app.weather.temperature_units import TemperatureUnit


def _keyboard_layout(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.keyboard]


def _sample_dict(item) -> dict:
    return {
        "market_title": item.market_title,
        "market_type": item.temperature_type_label,
        "market_unit_source": item.market_unit_source,
        "market_unit": item.market_unit,
        "provider_unit": item.provider_unit,
        "conversion_applied": item.conversion_applied,
        "forecast": item.forecast_display,
        "selected_outcome": item.best_outcome,
        "selected_live_price": item.raw_yes_price,
        "calculated_model_probability": item.model_probability,
        "comparison_text": item.comparison_text,
        "final_displayed_unit": item.displayed_unit,
        "final_signal": item.recommendation_tier,
        "report": format_concise_opportunity(item),
    }


def _fahrenheit_fixture_sample(settings) -> dict:
    """Realistic stored fixture when no live US Fahrenheit market is in the scan window."""
    raw = {
        "id": "fixture-chicago",
        "slug": "highest-temperature-in-chicago-on-july-14-2026-85-86f",
        "question": "Will the highest temperature in Chicago be 85-86°F on July 14?",
        "description": "Resolved using Weather Underground for station KORD in degrees Fahrenheit.",
        "rules": "Daily high temperature at KORD in Fahrenheit.",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.39", "0.61"],
        "groupItemTitle": "85-86°F",
        "events": [{
            "title": "Highest temperature in Chicago on July 14?",
            "description": "Daily high temperature at Chicago O'Hare (KORD) in Fahrenheit.",
        }],
    }
    market = parse_market(raw)
    item = analyze_market(settings, market)
    sample = _sample_dict(item)
    sample["fixture"] = True
    return sample


def main() -> None:
    settings = load_settings()
    progress = scan_weather_markets(settings)

    celsius = next(
        (item for item in progress.results if item.market_unit == TemperatureUnit.CELSIUS.value),
        None,
    )
    fahrenheit = next(
        (item for item in progress.results if item.market_unit == TemperatureUnit.FAHRENHEIT.value),
        None,
    )

    output = {
        "discovered": progress.discovered,
        "accepted": progress.accepted,
        "skipped": progress.skipped,
        "results_count": len(progress.results),
        "main_keyboard": _keyboard_layout(main_menu_keyboard()),
        "weather_submenu": _keyboard_layout(weather_menu_keyboard()),
        "celsius_sample": _sample_dict(celsius) if celsius else None,
        "fahrenheit_sample": _sample_dict(fahrenheit) if fahrenheit else None,
        "used_fixture_for_celsius": celsius is None,
        "used_fixture_for_fahrenheit": fahrenheit is None,
    }

    if celsius is None:
        fixture_path = Path("tests/fixtures/polymarket_munich.json")
        if fixture_path.exists():
            output["celsius_fixture_note"] = str(fixture_path)

    if fahrenheit is None:
        output["fahrenheit_sample"] = _fahrenheit_fixture_sample(settings)
        output["used_fixture_for_fahrenheit"] = True
        output["fahrenheit_fixture_note"] = "No live Fahrenheit market in scan window; used realistic Chicago fixture."

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
