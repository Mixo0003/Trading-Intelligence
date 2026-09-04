"""Official MLB Stats API — no key required."""

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

_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&hydrate=linescore,team"
)


class MlbStatsProvider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        if identity.category != LiveEdgeCategory.BASEBALL:
            return ()
        payload = fetch_json(_URL, cache_key="mlb-schedule", cache_ttl=15.0, provider="mlb-statsapi")
        if not isinstance(payload, dict):
            return ()
        found: list[LiveStateSnapshot] = []
        for day in payload.get("dates") or []:
            if not isinstance(day, dict):
                continue
            for game in day.get("games") or []:
                if not isinstance(game, dict):
                    continue
                snap = _game_to_snapshot(game, now)
                if snap is not None:
                    found.append(snap)
        return tuple(found)


def _game_to_snapshot(game: dict[str, Any], now: datetime) -> LiveStateSnapshot | None:
    status = game.get("status") if isinstance(game.get("status"), dict) else {}
    state = str(status.get("abstractGameState") or "")
    detailed = str(status.get("detailedState") or "")
    live = state.lower() == "live" or "in progress" in detailed.lower()
    if not live:
        return None
    teams = game.get("teams") if isinstance(game.get("teams"), dict) else {}
    home = _team(teams.get("home"))
    away = _team(teams.get("away"))
    if home is None or away is None:
        return None
    game_pk = str(game.get("gamePk") or "")
    if not game_pk:
        return None
    home_score = _int((teams.get("home") or {}).get("score"))
    away_score = _int((teams.get("away") or {}).get("score"))
    score = None
    if home_score is not None and away_score is not None:
        score = (home_score, away_score)
    linescore = game.get("linescore") if isinstance(game.get("linescore"), dict) else {}
    inning = _int(linescore.get("currentInning"))
    inning_state = str(linescore.get("inningState") or "") or None
    outs = _int(linescore.get("outs"))
    offense = linescore.get("offense") if isinstance(linescore.get("offense"), dict) else {}
    runners = ",".join(
        key for key in ("first", "second", "third") if offense.get(key)
    ) or None
    start = game.get("gameDate")
    start_time = None
    if isinstance(start, str):
        try:
            start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            start_time = None
    return LiveStateSnapshot(
        sport="baseball",
        status="live",
        participants=(home[0], away[0]),
        aliases=tuple(item for item in (home[1], away[1]) if item),
        competition="MLB",
        start_time=start_time,
        score=score,
        inning=inning,
        top_bottom=inning_state.lower() if inning_state else None,
        outs=outs,
        runners=runners,
        extra={"home_score": home_score, "away_score": away_score, "game_pk": game_pk},
        provenance=LiveDataProvenance(
            provider="mlb-statsapi",
            event_identifier=game_pk,
            source_locator=_URL,
            fetched_at=now,
            source_timestamp=now,
            reliability="official",
        ),
    )


def _team(blob: Any) -> tuple[str, str] | None:
    if not isinstance(blob, dict):
        return None
    team = blob.get("team") if isinstance(blob.get("team"), dict) else {}
    name = str(team.get("name") or team.get("teamName") or "").strip()
    abbr = str(team.get("abbreviation") or team.get("fileCode") or "").strip()
    if not name:
        return None
    return name, abbr


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
