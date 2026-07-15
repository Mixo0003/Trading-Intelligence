"""Read-only live proof: one real Polymarket weather event end to end.

Uses the exact production scan_service/scan_cache/formatting functions that
the Telegram /scan, Forecasts, and Top Signals handlers call. Does not place
trades, does not modify .env, does not write outside the local SQLite cache
that Telegram itself already uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings
from app.region.types import ScanRegion
from app.signals.scan_service import scan_weather_markets
from app.telegram.scan_cache import load_latest_scan_snapshot, save_scan_snapshot
from app.telegram.formatting import (
    format_scan_report,
    format_today_forecast_from_snapshot,
    format_top_signals_report,
)


def main() -> None:
    settings = load_settings()
    region = ScanRegion.ASIA
    telegram_id = 999999001
    bot_data: dict = {}

    progress = scan_weather_markets(settings, region=region)
    save_scan_snapshot(settings, telegram_id, progress, bot_data=bot_data, region=region)

    print("=== SCAN COUNTERS ===")
    print("parent_events_discovered=", progress.parent_events_discovered)
    print("child_markets_discovered=", progress.child_markets_discovered)
    print("parent_events_in_region=", progress.parent_events_in_region)
    print("child_markets_loaded_for_analysis=", progress.child_markets_loaded_for_analysis)
    print("child_markets_analyzed=", progress.child_markets_analyzed)
    print("parent_events_analyzed=", progress.parent_events_analyzed)
    print("results_count=", len(progress.results))
    print("all_analyses_count=", len(progress.all_analyses))

    analyses = progress.all_analyses or progress.results
    chosen = next((a for a in analyses if a.forecast_display), None) or (
        analyses[0] if analyses else None
    )

    print()
    print("=== TELEGRAM /scan OUTPUT ===")
    print(format_scan_report(progress).encode("ascii", "backslashreplace").decode("ascii"))

    reloaded = load_latest_scan_snapshot(settings, telegram_id, bot_data=bot_data, region=region)
    print()
    print("=== TELEGRAM Forecasts OUTPUT ===")
    print(
        format_today_forecast_from_snapshot(reloaded)
        .encode("ascii", "backslashreplace")
        .decode("ascii")
    )

    print()
    print("=== TELEGRAM Top Signals OUTPUT ===")
    print(
        format_top_signals_report(reloaded)
        .encode("ascii", "backslashreplace")
        .decode("ascii")
    )

    if chosen:
        print()
        print("=== ONE EVENT FULL TRACE ===")
        print("market_title=", chosen.market_title)
        print("market_url=", chosen.market_url)
        print("city_location=", chosen.city_location)
        print("station_code=", chosen.station_code)
        print("resolution_station=", chosen.resolution_station)
        print("resolution_source=", chosen.resolution_source)
        print("temperature_type_label=", chosen.temperature_type_label)
        print("target_date=", chosen.target_date)
        print("target_date_local=", chosen.target_date_local)
        print("station_timezone=", chosen.station_timezone)
        print("forecast_display=", chosen.forecast_display)
        print("expected_range_display=", chosen.expected_range_display)
        print("forecast_providers=", chosen.forecast_providers)
        print("forecast_gateway=", chosen.forecast_gateway)
        print("forecast_models=", chosen.forecast_models)
        print("provider_spread_display=", chosen.provider_spread_display)
        print("source_divergence_display=", chosen.source_divergence_display)
        print("best_outcome=", chosen.best_outcome)
        print("raw_yes_price=", chosen.raw_yes_price)
        print("implied_probability=", chosen.implied_probability)
        print("model_probability=", chosen.model_probability)
        print("edge=", chosen.edge)
        print("display_status=", chosen.display_status)
        print("display_reason=", chosen.display_reason)
        print("validation_error=", chosen.validation_error)
    else:
        print()
        print("NO_ANALYSES_PRODUCED")


if __name__ == "__main__":
    main()
