"""OpenLigaDB official German football match data. No key."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.opportunity_intel.live_edge.http_json import fetch_json
from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)

_URL = "https://api.openligadb.de/getmatchdata/bl1"


class OpenLigaDbProvider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        if identity.category != LiveEdgeCategory.SOCCER:
            return ()
        payload = fetch_json(_URL, cache_key="openligadb-bl1", cache_ttl=30.0, provider="openligadb")
        if not isinstance(payload, list):
            return ()
        found: list[LiveStateSnapshot] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            snap = _row_to_snapshot(row, now)
            if snap is not None:
                found.append(snap)
        return tuple(found)


def _row_to_snapshot(row: dict[str, Any], now: datetime) -> LiveStateSnapshot | None:
    if row.get("matchIsFinished") is True:
        return None
    results = row.get("matchResults")
    if not isinstance(results, list) or not results:
        return None
    live_result = None
    for item in results:
        if isinstance(item, dict) and item.get("resultTypeID") in {1, 2}:
            live_result = item
    if live_result is None:
        live_result = results[-1] if isinstance(results[-1], dict) else None
    if not isinstance(live_result, dict):
        return None
    team1 = (row.get("team1") or {}).get("teamName") if isinstance(row.get("team1"), dict) else None
    team2 = (row.get("team2") or {}).get("teamName") if isinstance(row.get("team2"), dict) else None
    if not team1 or not team2:
        return None
    try:
        score = (int(live_result.get("pointsTeam1")), int(live_result.get("pointsTeam2")))
    except (TypeError, ValueError):
        return None
    match_id = str(row.get("matchID") or "")
    if not match_id:
        return None
    start = None
    raw_start = row.get("matchDateTimeUTC")
    if isinstance(raw_start, str):
        try:
            start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        except ValueError:
            start = None
    return LiveStateSnapshot(
        sport="soccer",
        status="live",
        participants=(str(team1), str(team2)),
        aliases=(),
        competition="Bundesliga",
        start_time=start,
        score=score,
        extra={"home_score": score[0], "away_score": score[1]},
        provenance=LiveDataProvenance(
            provider="openligadb",
            event_identifier=match_id,
            source_locator=_URL,
            fetched_at=now,
            source_timestamp=now,
            reliability="official",
        ),
    )
