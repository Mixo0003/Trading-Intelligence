"""Liquipedia MediaWiki cargo — series scores only, never invented in-map stats.

Verified 2026-08-31: leagueoflegends wiki returns
``Unrecognized value for parameter action: cargoquery``.
This provider therefore fail-closes until cargo (or another structured
endpoint) is available.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.opportunity_intel.live_edge.http_json import LIVE_EDGE_USER_AGENT, fetch_json
from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)

_WIKIS = {
    LiveEdgeCategory.ESPORTS_LOL: (
        "https://liquipedia.net/leagueoflegends/api.php",
        "League of Legends",
    ),
    LiveEdgeCategory.ESPORTS_CS2: (
        "https://liquipedia.net/counterstrike/api.php",
        "Counter-Strike 2",
    ),
    LiveEdgeCategory.ESPORTS_DOTA: (
        "https://liquipedia.net/dota2/api.php",
        "Dota 2",
    ),
}


class LiquipediaMatchProvider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        spec = _WIKIS.get(identity.category)
        if spec is None:
            return ()
        base, sport = spec
        payload = _cargo(base)
        if not isinstance(payload, dict):
            return ()
        rows = payload.get("cargoquery")
        if not isinstance(rows, list):
            return ()
        found: list[LiveStateSnapshot] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            title = item.get("title") if isinstance(item.get("title"), dict) else item
            if not isinstance(title, dict):
                continue
            snap = _row_to_snapshot(title, sport, base, now)
            if snap is not None:
                found.append(snap)
        return tuple(found)


def _cargo(base: str) -> Any | None:
    params = (
        "action=cargoquery&format=json"
        "&tables=MatchSchedule"
        "&fields=Opponent,Opponent2,Bestof,Date,Winner,Status,Score"
        "&limit=50"
    )
    url = f"{base}?{params}"
    return fetch_json(
        url,
        headers={"Api-User-Agent": LIVE_EDGE_USER_AGENT},
        cache_key=f"liquipedia:{base}",
        cache_ttl=30.0,
        min_interval_seconds=1.0,
        provider="liquipedia",
    )


def _row_to_snapshot(
    row: dict[str, Any], sport: str, base: str, now: datetime
) -> LiveStateSnapshot | None:
    a = str(row.get("Opponent") or row.get("opponent") or "").strip()
    b = str(row.get("Opponent2") or row.get("opponent2") or "").strip()
    if not a or not b:
        return None
    status = str(row.get("Status") or row.get("status") or "").lower()
    if status and status not in {"live", "ongoing", "running", "started", ""}:
        if "upcoming" in status or "finished" in status or "done" in status:
            if "live" not in status and "ongoing" not in status:
                return None
    best_of = _best_of(row.get("Bestof") or row.get("bestof"))
    maps_won = _score(row.get("Score") or row.get("score"))
    event_id = f"{a}:{b}:{row.get('Date') or ''}"
    current_map = None
    if maps_won is not None:
        current_map = maps_won[0] + maps_won[1] + 1
    return LiveStateSnapshot(
        sport=sport,
        status="live" if "live" in status or not status else status,
        participants=(a, b),
        aliases=(),
        series_format=f"BO{best_of}" if best_of else None,
        maps_won=maps_won,
        current_map=current_map,
        map_finished=False,
        extra={"liquipedia_status": status, "raw": {k: row.get(k) for k in ("Date", "Winner")}},
        provenance=LiveDataProvenance(
            provider="liquipedia",
            event_identifier=event_id[:180],
            source_locator=base,
            fetched_at=now,
            source_timestamp=now,
            reliability="structured_wiki",
        ),
    )


def _best_of(value: Any) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).upper().replace("BO", "").strip()
    try:
        parsed = int(text)
    except ValueError:
        return None
    if parsed in {1, 3, 5, 7}:
        return parsed
    return None


def _score(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    text = str(value).replace(":", "-").replace("–", "-")
    parts = [part.strip() for part in text.split("-") if part.strip()]
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None
