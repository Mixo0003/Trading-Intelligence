"""BO3.gg structured CS2 match API. No HTML scraping.

Verified filter (CS2API / website widget):
``filter[matches.status][in]=current`` with ``scope=widget-matches``.
Unfiltered ``/matches`` ignores query params and returns historical page 1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from app.opportunity_intel.live_edge.health import record_snapshot_counts
from app.opportunity_intel.live_edge.http_json import fetch_json
from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)

_PROVIDER = "bo3-cs2"
_BASE = "https://api.bo3.gg/api/v1/matches"


class Bo3Cs2Provider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        _ = market
        if identity.category != LiveEdgeCategory.ESPORTS_CS2:
            return ()
        payload = fetch_json(
            _matches_url(),
            cache_key="bo3-cs2-current",
            cache_ttl=12.0,
            provider=_PROVIDER,
        )
        if not isinstance(payload, dict):
            record_snapshot_counts(_PROVIDER, live_events=0, named_events=0)
            return ()
        rows = payload.get("results")
        if not isinstance(rows, list):
            record_snapshot_counts(_PROVIDER, live_events=0, named_events=0)
            return ()
        found: list[LiveStateSnapshot] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            snap = _row_to_snapshot(row, now)
            if snap is not None:
                found.append(snap)
        live_n = sum(1 for item in found if item.status == "live")
        record_snapshot_counts(
            _PROVIDER, live_events=live_n, named_events=len(found)
        )
        return tuple(found)


def _matches_url() -> str:
    query = urlencode(
        {
            "scope": "widget-matches",
            "page[offset]": "0",
            "page[limit]": "50",
            "sort": "tier_rank,-start_date",
            "filter[matches.status][in]": "current",
            "filter[matches.discipline_id][eq]": "1",
            "with": "teams,tournament,games",
        }
    )
    return f"{_BASE}?{query}"


def _row_to_snapshot(row: dict[str, Any], now: datetime) -> LiveStateSnapshot | None:
    status = str(row.get("status") or "").lower()
    if status not in {"current", "live", "ongoing"}:
        return None
    names = _team_names(row)
    if len(names) < 2:
        return None
    match_id = str(row.get("id") or "")
    if not match_id:
        return None
    maps_won = _pair(row.get("team1_score"), row.get("team2_score"))
    games = row.get("games") if isinstance(row.get("games"), list) else []
    current_map, round_score, map_name, overtime = _from_games(games, maps_won)
    if current_map is None and maps_won is not None:
        current_map = maps_won[0] + maps_won[1] + 1
    tournament = _tournament_name(row)
    start = _parse_dt(row.get("start_date"))
    best_of = row.get("bo_type")
    series_format = f"BO{best_of}" if isinstance(best_of, int) and best_of in {1, 3, 5, 7} else None
    aliases = tuple(slug for slug in _team_slugs(row) if slug)
    extra = {
        "map_name": map_name,
        "bo3_id": match_id,
        "live_coverage": row.get("live_coverage"),
    }
    snapshot = _try_live_snapshot(match_id, now)
    if snapshot is not None:
        if snapshot.get("round_score") is not None:
            round_score = snapshot["round_score"]
        extra["snapshot_keys"] = snapshot.get("keys")
        if snapshot.get("overtime") is not None:
            overtime = snapshot["overtime"]
        if snapshot.get("side"):
            extra["side"] = snapshot["side"]
    return LiveStateSnapshot(
        sport="Counter-Strike 2",
        status="live",
        participants=(names[0], names[1]),
        aliases=aliases,
        competition=tournament,
        event_date=start,
        start_time=start,
        series_format=series_format,
        maps_won=maps_won,
        current_map=current_map,
        map_finished=False,
        round_score=round_score,
        overtime=overtime,
        extra=extra,
        provenance=LiveDataProvenance(
            provider=_PROVIDER,
            event_identifier=match_id,
            source_locator=_BASE,
            fetched_at=now,
            source_timestamp=now,
            reliability="structured_public",
        ),
    )


def _try_live_snapshot(match_id: str, now: datetime) -> dict[str, Any] | None:
    _ = now
    payload = fetch_json(
        f"https://api.bo3.gg/api/v1/live/matches/{match_id}/last_snapshot",
        cache_key=f"bo3-cs2-snap:{match_id}",
        cache_ttl=8.0,
    )
    if not isinstance(payload, dict):
        return None
    round_score = _pair(
        payload.get("team1_score") or payload.get("score1") or payload.get("ct_score"),
        payload.get("team2_score") or payload.get("score2") or payload.get("t_score"),
    )
    if round_score is None:
        current = payload.get("current_score") or payload.get("score")
        if isinstance(current, dict):
            round_score = _pair(current.get("team1"), current.get("team2"))
        elif isinstance(current, list) and len(current) >= 2:
            round_score = _pair(current[0], current[1])
    overtime = payload.get("overtime")
    if isinstance(overtime, bool):
        ot = overtime
    else:
        ot = None
    side = payload.get("side") if isinstance(payload.get("side"), str) else None
    return {
        "round_score": round_score,
        "overtime": ot,
        "side": side,
        "keys": tuple(sorted(payload.keys())[:24]),
    }


def _team_names(row: dict[str, Any]) -> list[str]:
    found: list[str] = []
    teams = row.get("teams")
    if isinstance(teams, list):
        for item in teams:
            if not isinstance(item, dict):
                continue
            nested = item.get("team") if isinstance(item.get("team"), dict) else item
            name = str(nested.get("name") or nested.get("title") or "").strip()
            if name:
                found.append(name)
    return found[:2]


def _team_slugs(row: dict[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    teams = row.get("teams")
    if isinstance(teams, list):
        for item in teams:
            if not isinstance(item, dict):
                continue
            nested = item.get("team") if isinstance(item.get("team"), dict) else item
            slug = str(nested.get("slug") or "").replace("-", " ").strip()
            if slug:
                found.append(slug)
    return tuple(found)


def _tournament_name(row: dict[str, Any]) -> str | None:
    tournament = row.get("tournament")
    if isinstance(tournament, dict):
        name = str(tournament.get("name") or "").strip()
        return name or None
    return None


def _from_games(
    games: list[Any], maps_won: tuple[int, int] | None
) -> tuple[int | None, tuple[int, int] | None, str | None, bool | None]:
    _ = maps_won
    live_index = None
    round_score = None
    map_name = None
    overtime = None
    for index, game in enumerate(games, start=1):
        if not isinstance(game, dict):
            continue
        state = str(game.get("state") or game.get("status") or "").lower()
        if state in {"live", "current", "ongoing", "running"}:
            live_index = index
            map_name = str(game.get("map_name") or game.get("name") or "") or None
            round_score = _pair(
                game.get("team1_score") or game.get("score1"),
                game.get("team2_score") or game.get("score2"),
            )
            break
    return live_index, round_score, map_name, overtime


def _pair(a: Any, b: Any) -> tuple[int, int] | None:
    try:
        if a is None or b is None or a == "" or b == "":
            return None
        return int(a), int(b)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
