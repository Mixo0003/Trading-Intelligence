"""Live read-only verification of regional weather scans."""

from __future__ import annotations

import json
import sys
import time

from app.config.settings import load_settings
from app.region.types import ScanRegion
from app.signals.forecast_display import (
    DISPLAY_VALIDATED,
    DISPLAY_WATCH,
    has_forecast_result,
)
from app.signals.scan_service import scan_weather_markets


def _summarize(region: ScanRegion) -> dict:
    settings = load_settings()
    started = time.perf_counter()
    progress = scan_weather_markets(settings, region=region)
    elapsed = time.monotonic() - started

    analyses = progress.all_analyses or progress.results
    forecasts = [
        item for item in analyses
        if has_forecast_result(item.display_status, item.forecast_display)
    ]
    watchlist = [item for item in analyses if item.display_status == DISPLAY_WATCH]
    validated = [item for item in analyses if item.display_status == DISPLAY_VALIDATED]

    metrics = progress.metrics
    return {
        "region": region.value,
        "parent_events_discovered": progress.parent_events_discovered,
        "child_markets_discovered": progress.child_markets_discovered,
        "parent_events_retained": progress.parent_events_in_region,
        "child_markets_loaded_for_analysis": (
            progress.child_markets_loaded_for_analysis
        ),
        "parent_events_analyzed": progress.parent_events_analyzed,
        "forecasts_produced": len(forecasts),
        "watchlist_results": len(watchlist),
        "validated_opportunities": len(validated),
        "skip_reasons": progress.skip_reasons,
        "polymarket_requests": metrics.polymarket_requests if metrics else 0,
        "weather_requests": metrics.weather_requests if metrics else 0,
        "reference_requests": metrics.reference_requests if metrics else 0,
        "anthropic_requests": metrics.anthropic_requests if metrics else 0,
        "excluded_before_weather": metrics.markets_excluded_before_weather if metrics else 0,
        "duration_seconds": round(elapsed, 2),
        "sample_cities": [item.city_location for item in analyses[:5]],
    }


def main() -> int:
    results = []
    for region in (ScanRegion.USA, ScanRegion.EUROPE, ScanRegion.ASIA, ScanRegion.ALL):
        print(f"Running {region.value} scan...", file=sys.stderr)
        results.append(_summarize(region))

    all_weather = next(item["weather_requests"] for item in results if item["region"] == "ALL")
    print(json.dumps({"scans": results, "all_weather_requests": all_weather}, indent=2))

    for item in results:
        if item["region"] != "ALL" and item["weather_requests"] >= all_weather:
            print(
                f"WARNING: {item['region']} weather requests ({item['weather_requests']}) "
                f"not less than ALL ({all_weather})",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
