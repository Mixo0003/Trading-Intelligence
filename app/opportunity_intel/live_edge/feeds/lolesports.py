"""Riot LoL Esports persisted-gw + livestats window. Key optional; 403 fails closed.

Public website ``x-api-key`` is tried when ``LIVE_EDGE_LOLESPORTS_API_KEY`` is
empty. Verified 2026-08-31: ``getLive`` / ``getSchedule`` return HTTP 403.
``feed.lolesports.com/livestats/v1/window/{gameId}`` is reachable (404 for an
unknown id, not 403) but needs a live game id from getLive/schedule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.opportunity_intel.live_edge.health import record_snapshot_counts
from app.opportunity_intel.live_edge.http_json import fetch_json
from app.opportunity_intel.live_edge.keys import lolesports_api_key
from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)

_PROVIDER = "lolesports"
_GET_LIVE = "https://esports-api.lolesports.com/persisted/gw/getLive?hl=en-US"
_WINDOW = "https://feed.lolesports.com/livestats/v1/window/{game_id}"
# Documented public website client id, not a user secret.
_PUBLIC_WEBSITE_KEY = "0TvQweyHBbTbGzGM6TejAHKDzj2AV9O4"


class LoLEsportsProvider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        _ = market
        if identity.category != LiveEdgeCategory.ESPORTS_LOL:
            return ()
        key = lolesports_api_key() or _PUBLIC_WEBSITE_KEY
        payload = fetch_json(
            _GET_LIVE,
            headers={
                "x-api-key": key,
                "Origin": "https://lolesports.com",
                "Referer": "https://lolesports.com/",
            },
            cache_key="lolesports-getlive",
            cache_ttl=12.0,
            provider=_PROVIDER,
        )
        if not isinstance(payload, dict):
            record_snapshot_counts(_PROVIDER, live_events=0, named_events=0)
            return ()
        events = _events(payload)
        found: list[LiveStateSnapshot] = []
        for event in events:
            snap = _event_to_snapshot(event, now)
            if snap is not None:
                found.append(snap)
        record_snapshot_counts(
            _PROVIDER, live_events=len(found), named_events=len(found)
        )
        return tuple(found)


def _events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    schedule = data.get("schedule") if isinstance(data.get("schedule"), dict) else data
    events = schedule.get("events") if isinstance(schedule, dict) else None
    if isinstance(events, list):
        return [item for item in events if isinstance(item, dict)]
    return []


def _event_to_snapshot(event: dict[str, Any], now: datetime) -> LiveStateSnapshot | None:
    match = event.get("match") if isinstance(event.get("match"), dict) else {}
    teams = match.get("teams") if isinstance(match.get("teams"), list) else []
    names: list[str] = []
    maps: list[int] = []
    for team in teams:
        if not isinstance(team, dict):
            continue
        name = str(team.get("name") or team.get("code") or "").strip()
        if name:
            names.append(name)
        result = team.get("result") if isinstance(team.get("result"), dict) else {}
        if result.get("gameWins") is not None:
            try:
                maps.append(int(result["gameWins"]))
            except (TypeError, ValueError):
                pass
    if len(names) < 2:
        return None
    event_id = str(event.get("id") or match.get("id") or "")
    if not event_id:
        return None
    maps_won = (maps[0], maps[1]) if len(maps) >= 2 else None
    games = match.get("games") if isinstance(match.get("games"), list) else []
    current_map = None
    live_game_id = None
    for index, game in enumerate(games, start=1):
        if not isinstance(game, dict):
            continue
        state = str(game.get("state") or "").lower()
        if state in {"inprogress", "in_progress", "live"}:
            current_map = index
            live_game_id = str(game.get("id") or "") or None
            break
    if current_map is None and maps_won is not None:
        current_map = maps_won[0] + maps_won[1] + 1
    league = event.get("league") if isinstance(event.get("league"), dict) else {}
    tournament = str(league.get("name") or "").strip() or None
    kills = gold = towers = None
    dragons = barons = inhibitors = None
    game_time = None
    if live_game_id:
        window = fetch_json(
            _WINDOW.format(game_id=live_game_id),
            cache_key=f"lolesports-window:{live_game_id}",
            cache_ttl=8.0,
            provider=_PROVIDER,
        )
        parsed = _parse_window(window)
        kills = parsed.get("kills")
        gold = parsed.get("gold")
        towers = parsed.get("towers")
        dragons = parsed.get("dragons")
        barons = parsed.get("barons")
        inhibitors = parsed.get("inhibitors")
        game_time = parsed.get("game_time")
    return LiveStateSnapshot(
        sport="League of Legends",
        status="live",
        participants=(names[0], names[1]),
        aliases=(),
        competition=tournament,
        series_format=None,
        maps_won=maps_won,
        current_map=current_map,
        map_finished=False,
        kills=kills,
        gold_difference=gold,
        towers=towers,
        dragons=dragons,
        barons=barons,
        inhibitors=inhibitors,
        game_time_seconds=game_time,
        extra={"lolesports_event": event_id, "game_id": live_game_id},
        provenance=LiveDataProvenance(
            provider=_PROVIDER,
            event_identifier=event_id,
            source_locator=_GET_LIVE,
            fetched_at=now,
            source_timestamp=now,
            reliability="official_esports",
        ),
    )


def _parse_window(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    frames = payload.get("frames")
    frame = None
    if isinstance(frames, list) and frames:
        last = frames[-1]
        if isinstance(last, dict):
            frame = last
    if frame is None and isinstance(payload.get("frame"), dict):
        frame = payload["frame"]
    if not isinstance(frame, dict):
        return {}
    blue = frame.get("blueTeam") if isinstance(frame.get("blueTeam"), dict) else {}
    red = frame.get("redTeam") if isinstance(frame.get("redTeam"), dict) else {}
    out: dict[str, Any] = {}
    try:
        if blue.get("totalKills") is not None and red.get("totalKills") is not None:
            out["kills"] = (int(blue["totalKills"]), int(red["totalKills"]))
        if blue.get("totalGold") is not None and red.get("totalGold") is not None:
            out["gold"] = int(blue["totalGold"]) - int(red["totalGold"])
        if blue.get("towers") is not None and red.get("towers") is not None:
            out["towers"] = (int(blue["towers"]), int(red["towers"]))
        if blue.get("dragons") is not None:
            out["dragons"] = len(blue["dragons"]) if isinstance(blue["dragons"], list) else int(blue["dragons"])
        if blue.get("barons") is not None and red.get("barons") is not None:
            out["barons"] = int(blue["barons"])
        if blue.get("inhibitors") is not None and red.get("inhibitors") is not None:
            out["inhibitors"] = (int(blue["inhibitors"]), int(red["inhibitors"]))
        rfc = frame.get("rfc460Timestamp") or payload.get("esportsGameId")
        _ = rfc
        gt = frame.get("gameTime") or frame.get("gameTimeMillis")
        if isinstance(gt, (int, float)):
            out["game_time"] = int(gt // 1000) if gt > 10_000 else int(gt)
    except (TypeError, ValueError):
        return out
    return out
