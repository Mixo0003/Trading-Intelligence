"""PandaScore running-matches adapter. Key optional; missing key fails closed."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.opportunity_intel.live_edge.health import record_auth_required, record_snapshot_counts
from app.opportunity_intel.live_edge.http_json import fetch_json
from app.opportunity_intel.live_edge.keys import pandascore_token
from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)

_PROVIDER = "pandascore"
_ENDPOINTS = {
    LiveEdgeCategory.ESPORTS_LOL: (
        "https://api.pandascore.co/lol/matches/running",
        "League of Legends",
    ),
    LiveEdgeCategory.ESPORTS_CS2: (
        "https://api.pandascore.co/csgo/matches/running",
        "Counter-Strike 2",
    ),
    LiveEdgeCategory.ESPORTS_DOTA: (
        "https://api.pandascore.co/dota2/matches/running",
        "Dota 2",
    ),
}


class PandaScoreProvider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        _ = market
        spec = _ENDPOINTS.get(identity.category)
        if spec is None:
            return ()
        token = pandascore_token()
        if not token:
            record_auth_required(_PROVIDER, "LIVE_ESPORTS_PROVIDER_API_KEY missing")
            record_snapshot_counts(_PROVIDER, live_events=0, named_events=0)
            return ()
        url, sport = spec
        payload = fetch_json(
            url,
            headers={"Authorization": f"Bearer {token}"},
            cache_key=f"pandascore:{identity.category.value}",
            cache_ttl=12.0,
            provider=_PROVIDER,
        )
        if not isinstance(payload, list):
            record_snapshot_counts(_PROVIDER, live_events=0, named_events=0)
            return ()
        found: list[LiveStateSnapshot] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            snap = _row_to_snapshot(row, sport, url, now)
            if snap is not None:
                found.append(snap)
        record_snapshot_counts(
            _PROVIDER, live_events=len(found), named_events=len(found)
        )
        return tuple(found)


def _row_to_snapshot(
    row: dict[str, Any], sport: str, url: str, now: datetime
) -> LiveStateSnapshot | None:
    names = _opponent_names(row)
    if len(names) < 2:
        return None
    match_id = str(row.get("id") or "")
    if not match_id:
        return None
    maps_won = _results_score(row)
    games = row.get("games") if isinstance(row.get("games"), list) else []
    current_map = None
    round_score = None
    game_time = None
    running_game: dict[str, Any] | None = None
    for index, game in enumerate(games, start=1):
        if not isinstance(game, dict):
            continue
        status = str(game.get("status") or "").lower()
        if status in {"running", "live", "current"}:
            current_map = int(game.get("position") or index)
            round_score = _pair(game.get("team1_score"), game.get("team2_score"))
            if isinstance(game.get("length"), (int, float)):
                game_time = int(game["length"])
            running_game = game
            break
    if current_map is None and maps_won is not None:
        current_map = maps_won[0] + maps_won[1] + 1
    tournament = ""
    if isinstance(row.get("tournament"), dict):
        tournament = str(row["tournament"].get("name") or "").strip()
    if not tournament and isinstance(row.get("league"), dict):
        tournament = str(row["league"].get("name") or "").strip()
    begin = _parse_dt(row.get("begin_at") or row.get("scheduled_at"))
    modified = _parse_dt(row.get("modified_at"))
    best_of = row.get("number_of_games")
    series_format = (
        f"BO{best_of}" if isinstance(best_of, int) and best_of in {1, 3, 5, 7} else None
    )
    live_meta = row.get("live") if isinstance(row.get("live"), dict) else {}
    in_map = _optional_in_map(row, running_game)
    extra = {
        "pandascore_id": match_id,
        "detailed_stats": row.get("detailed_stats"),
        "live_supported": live_meta.get("supported")
        if live_meta.get("supported") is not None
        else row.get("live_supported"),
        "in_map": bool(in_map),
    }
    return LiveStateSnapshot(
        sport=sport,
        status="live",
        participants=(names[0], names[1]),
        aliases=_acronyms(row),
        competition=tournament or None,
        event_date=begin,
        start_time=begin,
        series_format=series_format,
        maps_won=maps_won,
        current_map=current_map,
        map_finished=False,
        round_score=round_score if round_score is not None else in_map.get("round_score"),
        kills=in_map.get("kills"),
        gold_difference=in_map.get("gold"),
        towers=in_map.get("towers"),
        net_worth_difference=in_map.get("net_worth"),
        game_time_seconds=game_time,
        extra=extra,
        provenance=LiveDataProvenance(
            provider=_PROVIDER,
            event_identifier=match_id,
            source_locator=url,
            fetched_at=now,
            source_timestamp=modified or begin or now,
            reliability="structured_keyed",
        ),
    )


def _acronyms(row: dict[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    opponents = row.get("opponents")
    if isinstance(opponents, list):
        for item in opponents:
            if not isinstance(item, dict):
                continue
            opponent = item.get("opponent") if isinstance(item.get("opponent"), dict) else item
            acronym = str(opponent.get("acronym") or "").strip()
            name = str(opponent.get("name") or "").strip()
            if acronym and acronym != name:
                found.append(acronym)
    return tuple(found)


def _optional_in_map(
    row: dict[str, Any], running_game: dict[str, Any] | None
) -> dict[str, Any]:
    """Accept in-map metrics only when they are already structured on the payload.

    Free REST ``/matches/running`` does not include gold/kills/rounds. Paid
    WebSocket frames are not consumed here (no architecture change).
    """
    sources = [running_game or {}, row]
    live = row.get("live") if isinstance(row.get("live"), dict) else {}
    sources.append(live)
    for source in sources:
        blue = source.get("blue") if isinstance(source.get("blue"), dict) else {}
        red = source.get("red") if isinstance(source.get("red"), dict) else {}
        kills = _pair(blue.get("kills") or blue.get("totalKills"), red.get("kills") or red.get("totalKills"))
        gold_a = blue.get("gold") or blue.get("totalGold")
        gold_b = red.get("gold") or red.get("totalGold")
        gold = None
        try:
            if gold_a is not None and gold_b is not None:
                gold = int(gold_a) - int(gold_b)
        except (TypeError, ValueError):
            gold = None
        towers = _pair(blue.get("towers"), red.get("towers"))
        rounds = _pair(
            blue.get("round_score") or source.get("team1_score"),
            red.get("round_score") or source.get("team2_score"),
        )
        nw = None
        try:
            if blue.get("net_worth") is not None and red.get("net_worth") is not None:
                nw = int(blue["net_worth"]) - int(red["net_worth"])
        except (TypeError, ValueError):
            nw = None
        if kills or gold is not None or towers or rounds or nw is not None:
            return {
                "kills": kills,
                "gold": gold,
                "towers": towers,
                "round_score": rounds,
                "net_worth": nw,
            }
    return {}


def _opponent_names(row: dict[str, Any]) -> list[str]:
    found: list[str] = []
    opponents = row.get("opponents")
    if isinstance(opponents, list):
        for item in opponents:
            if not isinstance(item, dict):
                continue
            opponent = item.get("opponent") if isinstance(item.get("opponent"), dict) else item
            name = str(opponent.get("name") or opponent.get("acronym") or "").strip()
            if name:
                found.append(name)
    return found[:2]


def _results_score(row: dict[str, Any]) -> tuple[int, int] | None:
    results = row.get("results")
    if isinstance(results, list) and len(results) >= 2:
        scores = []
        for item in results[:2]:
            if isinstance(item, dict) and item.get("score") is not None:
                try:
                    scores.append(int(item["score"]))
                except (TypeError, ValueError):
                    return None
        if len(scores) == 2:
            return scores[0], scores[1]
    return None


def _pair(a: Any, b: Any) -> tuple[int, int] | None:
    try:
        if a is None or b is None:
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
