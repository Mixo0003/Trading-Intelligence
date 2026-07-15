"""Read-only scan diagnostic along the Telegram Scan Now path."""

from __future__ import annotations

import sys
from collections import Counter

from app.config.settings import load_settings
from app.polymarket.client import PolymarketClient
from app.polymarket.discovery import evaluate_market_eligibility
from app.polymarket.event_expansion import expand_group_children
from app.polymarket.markets import get_weather_markets
from app.signals.event_analysis import analyze_temperature_event
from app.signals.market_analysis import analyze_market
from app.signals.scan_service import _event_id, _group_markets_by_event
from app.telegram.formatting import format_scan_report


def _event_id_from_market(market) -> str | None:
    return _event_id(market)


def main() -> int:
    settings = load_settings()
    client = PolymarketClient(settings)
    seen: set[str] = set()

    markets, worldwide, priority = get_weather_markets(
        client,
        limit=settings.max_markets_per_scan,
        priority_cities=settings.priority_weather_cities,
    )
    grouped, standalone = _group_markets_by_event(markets)
    analyzed_events: set[str] = set()
    analyses = []
    discovery_skips: Counter[str] = Counter()
    accepted = 0

    def analyze_group(children):
        nonlocal accepted
        if not children:
            return
        eid = _event_id_from_market(children[0])
        if eid and eid in analyzed_events:
            return
        eligible = []
        for market in children:
            ok, reason, _ = evaluate_market_eligibility(market, seen)
            if ok:
                eligible.append(market)
            elif reason:
                discovery_skips[reason.value] += 1
        if not eligible:
            return
        accepted += len(eligible)
        expanded = expand_group_children(client, eligible)
        if len(expanded) > 1:
            analysis = analyze_temperature_event(settings, expanded)
        else:
            analysis = analyze_market(settings, expanded[0])
        if analysis is None:
            discovery_skips["analysis_none"] += 1
            return
        if eid:
            analyzed_events.add(eid)
        analyses.append(analysis)

    for children in grouped.values():
        analyze_group(children)
    for market in standalone:
        analyze_group([market])

    validation_skips: Counter[str] = Counter()
    for a in analyses:
        if a.recommendation_tier in {"SKIP", "WATCH"}:
            key = a.validation_error or a.recommendation_tier
            validation_skips[key] += 1

    actionable = [a for a in analyses if a.recommendation_tier not in {"SKIP", "WATCH"}]

    print("=== SCAN COUNTS ===")
    print(f"discovered: {len(markets)}")
    print(f"accepted: {accepted}")
    print(f"discovery_skipped: {sum(discovery_skips.values())}")
    print(f"completed_analyses: {len(analyses)}")
    print(f"actionable: {len(actionable)}")
    print(f"results_limit: {settings.max_results_per_report}")

    print("\n=== DISCOVERY SKIP REASONS ===")
    for reason, count in discovery_skips.most_common():
        print(f"  {reason}: {count}")

    print("\n=== VALIDATION / TIER SKIP REASONS ===")
    for reason, count in validation_skips.most_common():
        print(f"  {reason}: {count}")

    print("\n=== EVENT DIAGNOSTICS ===")
    for a in analyses:
        div = ""
        if a.forecast_diagnostics and "divergence:" in a.forecast_diagnostics.lower():
            div = a.forecast_diagnostics.split("divergence:")[-1].strip()
        ref = ""
        if a.forecast_diagnostics and "Reference forecast" in a.forecast_diagnostics:
            for line in a.forecast_diagnostics.splitlines():
                if "Reference forecast" in line:
                    ref = line.split(":", 1)[-1].strip()
        print(
            f"{a.city_location or '?'} | "
            f"{a.temperature_type_label or '?'} | "
            f"{a.target_date or '?'} | "
            f"{a.resolution_station or a.station_code or '?'} | "
            f"{a.forecast_providers or '?'} | "
            f"{a.forecast_display or 'N/A'} | "
            f"{ref or 'N/A'} | "
            f"{div or 'N/A'} | "
            f"{a.best_outcome} | "
            f"{a.recommendation_tier} | "
            f"{a.validation_error or '-'}"
        )

    from app.signals.market_analysis import ScanProgress

    progress = ScanProgress(
        discovered=len(markets),
        accepted=accepted,
        skipped=sum(discovery_skips.values()),
        skip_reasons=dict(discovery_skips),
        all_analyses=analyses,
        results=analyses[: settings.max_results_per_report],
        worldwide_discovered=worldwide,
        priority_us_discovered=priority,
    )
    print("\n=== TELEGRAM SCAN REPORT (current) ===")
    print(format_scan_report(progress).encode("ascii", "backslashreplace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
