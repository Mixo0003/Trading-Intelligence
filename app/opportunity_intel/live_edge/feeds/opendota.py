"""OpenDota public live games. Only rows with real team names (fail closed on pubs)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.opportunity_intel.live_edge.health import record_snapshot_counts
from app.opportunity_intel.live_edge.http_json import fetch_json
from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)

_URL = "https://api.opendota.com/api/live"


class OpenDotaLiveProvider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        if identity.category != LiveEdgeCategory.ESPORTS_DOTA:
            return ()
        payload = fetch_json(
            _URL, cache_key="opendota-live", cache_ttl=10.0, provider="opendota-live"
        )
        if not isinstance(payload, list):
            return ()
        found: list[LiveStateSnapshot] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            snap = _row_to_snapshot(row, now)
            if snap is not None:
                found.append(snap)
        record_snapshot_counts(
            "opendota-live", live_events=len(found), named_events=len(found)
        )
        return tuple(found)


def _row_to_snapshot(row: dict[str, Any], now: datetime) -> LiveStateSnapshot | None:
    radiant = _named_team(row, "team_name_radiant", "radiant_team", "radiant_name")
    dire = _named_team(row, "team_name_dire", "dire_team", "dire_name")
    if not radiant or not dire:
        return None
    match_id = str(row.get("match_id") or "")
    if not match_id:
        return None
    delay = int(row.get("delay") or 0)
    source_ts = now - timedelta(seconds=max(0, delay))
    scoreboard = row.get("scoreboard") if isinstance(row.get("scoreboard"), dict) else {}
    rad = scoreboard.get("radiant") if isinstance(scoreboard.get("radiant"), dict) else {}
    dire_board = scoreboard.get("dire") if isinstance(scoreboard.get("dire"), dict) else {}
    kills = None
    if rad.get("score") is not None and dire_board.get("score") is not None:
        try:
            kills = (int(rad["score"]), int(dire_board["score"]))
        except (TypeError, ValueError):
            kills = None
    nw = None
    try:
        r_nw = float(rad.get("gold") or rad.get("net_worth") or 0)
        d_nw = float(dire_board.get("gold") or dire_board.get("net_worth") or 0)
        if r_nw or d_nw:
            nw = int(r_nw - d_nw)
    except (TypeError, ValueError):
        nw = None
    towers = None
    if rad.get("tower_state") is not None and dire_board.get("tower_state") is not None:
        towers = (
            _bitcount(rad.get("tower_state")),
            _bitcount(dire_board.get("tower_state")),
        )
    barracks = None
    if rad.get("barracks_state") is not None and dire_board.get("barracks_state") is not None:
        barracks = (
            _bitcount(rad.get("barracks_state")),
            _bitcount(dire_board.get("barracks_state")),
        )
    game_time = row.get("game_time")
    game_time_seconds = int(game_time) if isinstance(game_time, (int, float)) else None
    series_id = str(row.get("series_id") or match_id)
    return LiveStateSnapshot(
        sport="Dota 2",
        status="live",
        participants=(radiant, dire),
        aliases=(),
        competition=str(row.get("league_id") or "") or None,
        series_format=None,
        maps_won=None,
        current_map=1,
        map_finished=False,
        kills=kills,
        net_worth_difference=nw,
        towers=towers,
        barracks=barracks,
        game_time_seconds=game_time_seconds,
        extra={"delay_seconds": delay, "match_id": match_id, "series_id": series_id},
        provenance=LiveDataProvenance(
            provider="opendota-live",
            event_identifier=match_id,
            source_locator=_URL,
            fetched_at=now,
            source_timestamp=source_ts,
            reliability="structured_public",
        ),
    )


def _named_team(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            name = str(value.get("name") or value.get("team_name") or "").strip()
            if name:
                return name
    return ""


def _bitcount(value: Any) -> int:
    try:
        return int(value).bit_count()
    except (TypeError, ValueError):
        return 0
