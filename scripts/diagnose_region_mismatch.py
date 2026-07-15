"""Read-only diagnostic: show why every discovered parent event is REGION_MISMATCH."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings
from app.polymarket.client import PolymarketClient
from app.polymarket.market_text import build_market_context_text
from app.polymarket.markets import get_weather_markets
from app.region.types import ScanRegion
from app.signals.scan_service import _classify_parent_event, _group_markets_by_event


def main() -> None:
    settings = load_settings()
    client = PolymarketClient(settings)
    markets, worldwide, priority = get_weather_markets(
        client,
        limit=settings.max_markets_per_scan,
        priority_cities=settings.priority_weather_cities,
    )
    grouped, standalone = _group_markets_by_event(markets)
    parent_groups = list(grouped.values()) + [[m] for m in standalone]

    print("worldwide_discovered=", worldwide)
    print("priority_added=", priority)
    print("parent_group_count=", len(parent_groups))
    print()

    for children in parent_groups:
        if not children:
            continue
        text = build_market_context_text(children[0])
        classification = _classify_parent_event(children)
        title = (children[0].question or "")[:80]
        print(f"title={title!r}")
        print(f"  region={classification.region} country={classification.country_code} "
              f"source={classification.source} conflict={classification.conflict}")
        print(f"  reason={classification.reason}")
        print(f"  evidence={classification.evidence}")
        print()


if __name__ == "__main__":
    main()
