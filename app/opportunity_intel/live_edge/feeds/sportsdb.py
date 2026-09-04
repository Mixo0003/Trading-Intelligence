"""TheSportsDB public livescore feed (documented free developer key, no user secret)."""

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

# Published by TheSportsDB as the free test/developer key. Not a user credential.
_PUBLIC_KEY = "3"
_BASE = f"https://www.thesportsdb.com/api/v1/json/{_PUBLIC_KEY}/livescore.php"

_SPORT = {
    LiveEdgeCategory.SOCCER: ("Soccer", "soccer"),
    LiveEdgeCategory.BASKETBALL: ("Basketball", "basketball"),
    LiveEdgeCategory.TENNIS: ("Tennis", "tennis"),
    LiveEdgeCategory.BASEBALL: ("Baseball", "baseball"),
}


class TheSportsDbProvider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        spec = _SPORT.get(identity.category)
        if spec is None:
            return ()
        sport_query, sport_label = spec
        payload = fetch_json(
            f"{_BASE}?s={sport_query}",
            cache_key=f"sportsdb:{sport_query}",
            cache_ttl=20.0,
            min_interval_seconds=1.0,
            provider="thesportsdb-livescore",
        )
        rows = payload.get("livescore") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return ()
        found: list[LiveStateSnapshot] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            snap = _row_to_snapshot(row, sport_label, now)
            if snap is not None:
                found.append(snap)
        return tuple(found)


def _row_to_snapshot(
    row: dict[str, Any], sport: str, now: datetime
) -> LiveStateSnapshot | None:
    home = str(row.get("strHomeTeam") or "").strip()
    away = str(row.get("strAwayTeam") or "").strip()
    if not home or not away:
        return None
    event_id = str(row.get("idEvent") or row.get("idLiveScore") or "")
    if not event_id:
        return None
    home_score = _int(row.get("intHomeScore"))
    away_score = _int(row.get("intAwayScore"))
    score = None
    if home_score is not None and away_score is not None:
        # Leader-first tuple for evaluators (index 0 = potential market side is remapped in matching).
        score = (home_score, away_score)
    status = str(row.get("strStatus") or "").strip()
    status_l = status.lower()
    if status_l in {"ft", "ns", "pst", "canc", "abd", "awarded", "finished", "postp", "cancl"}:
        return None
    progress = str(row.get("strProgress") or status).strip()
    if progress.lower() in {"ft", "finished"}:
        return None
    minute = _int(row.get("intTime") or progress)
    remaining = _remaining_seconds(sport, progress)
    provenance = LiveDataProvenance(
        provider="thesportsdb-livescore",
        event_identifier=event_id,
        source_locator=f"{_BASE}?s={sport}",
        fetched_at=now,
        source_timestamp=now,
        reliability="structured_public",
    )
    return LiveStateSnapshot(
        sport=sport,
        status=str(row.get("strStatus") or "live"),
        participants=(home, away),
        aliases=_aliases(home, away, row),
        competition=str(row.get("strLeague") or "") or None,
        start_time=now,
        score=score,
        period=progress or None,
        remaining_seconds=remaining,
        minute=minute if sport == "soccer" else None,
        extra={"home_score": home_score, "away_score": away_score, "raw_progress": progress},
        provenance=provenance,
    )


def _aliases(home: str, away: str, row: dict[str, Any]) -> tuple[str, ...]:
    extras = []
    for key in ("strHomeTeamShort", "strAwayTeamShort"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            extras.append(value.strip())
    return tuple(extras)


def _remaining_seconds(sport: str, progress: str) -> int | None:
    text = progress.lower()
    if sport == "basketball":
        if "ot" in text:
            return 120
        if "q4" in text or "4th" in text:
            return 180
        if "q3" in text or "3rd" in text:
            return 480
        if "q2" in text or "2nd" in text:
            return 900
        if "q1" in text or "1st" in text:
            return 1440
    return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
