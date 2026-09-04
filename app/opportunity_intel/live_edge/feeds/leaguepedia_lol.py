"""Leaguepedia (lol.fandom.com) cargoquery — named LoL schedule/live series state.

Verified 2026-08-31: ``action=cargoquery`` is enabled (unlike liquipedia.net).
Series scores and team names are editor-updated; in-map gold/kills are not
invented from this source.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from app.opportunity_intel.live_edge.health import record_http, record_snapshot_counts
from app.opportunity_intel.live_edge.http_json import fetch_json
from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)

_PROVIDER = "leaguepedia-lol"
_API = "https://lol.fandom.com/api.php"
_UA = (
    "TradingIntelligenceLiveEdge/2.4.4 "
    "(structured Leaguepedia cargoquery; analysis-only)"
)
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")


class LeaguepediaLolProvider:
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
        payload = fetch_json(
            _schedule_url(now),
            headers={"Api-User-Agent": _UA, "User-Agent": _UA},
            cache_key="leaguepedia-lol-schedule",
            cache_ttl=45.0,
            min_interval_seconds=1.0,
            provider=_PROVIDER,
        )
        if not isinstance(payload, dict):
            record_snapshot_counts(_PROVIDER, live_events=0, named_events=0)
            return ()
        if isinstance(payload.get("error"), dict):
            err = payload["error"]
            info = str(err.get("info") or err.get("code") or "cargo error")
            status = 429 if err.get("code") == "ratelimited" else 400
            record_http(_PROVIDER, status_code=status, error=info)
            record_snapshot_counts(_PROVIDER, live_events=0, named_events=0)
            return ()
        rows = payload.get("cargoquery")
        if not isinstance(rows, list):
            record_snapshot_counts(_PROVIDER, live_events=0, named_events=0)
            return ()
        found: list[LiveStateSnapshot] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            title = item.get("title") if isinstance(item.get("title"), dict) else item
            if not isinstance(title, dict):
                continue
            snap = _row_to_snapshot(title, now)
            if snap is not None:
                found.append(snap)
        live_n = sum(1 for item in found if item.status == "live")
        record_snapshot_counts(
            _PROVIDER, live_events=live_n, named_events=len(found)
        )
        return tuple(found)


def _schedule_url(now: datetime) -> str:
    clock = now.astimezone(timezone.utc)
    start = (clock - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    end = (clock + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    query = urlencode(
        {
            "action": "cargoquery",
            "format": "json",
            "tables": "MatchSchedule",
            "fields": "Team1,Team2,Team1Score,Team2Score,BestOf,DateTime_UTC,Winner,OverviewPage",
            "where": f"DateTime_UTC>='{start}' AND DateTime_UTC<='{end}'",
            "order_by": "DateTime_UTC",
            "limit": "50",
        }
    )
    return f"{_API}?{query}"


def _row_to_snapshot(row: dict[str, Any], now: datetime) -> LiveStateSnapshot | None:
    a = _field(row, "Team1")
    b = _field(row, "Team2")
    if not a or not b:
        return None
    winner = _field(row, "Winner")
    if winner in {"1", "2", "0"}:
        return None
    start = _parse_dt(_field(row, "DateTime_UTC", "DateTime UTC"))
    if start is None:
        return None
    clock = now.astimezone(timezone.utc)
    if start > clock + timedelta(minutes=15):
        status = "upcoming"
    else:
        status = "live"
    if start < clock - timedelta(hours=8):
        return None
    maps_won = _score(_field(row, "Team1Score"), _field(row, "Team2Score"))
    best_of = _best_of(_field(row, "BestOf"))
    current_map = None
    if status == "live":
        if maps_won is not None:
            current_map = maps_won[0] + maps_won[1] + 1
        else:
            current_map = 1
            maps_won = (0, 0)
    overview = _field(row, "OverviewPage")
    aliases = _aliases(a) + _aliases(b)
    event_id = f"{a}:{b}:{start.isoformat()}"
    return LiveStateSnapshot(
        sport="League of Legends",
        status=status,
        participants=(a, b),
        aliases=aliases,
        competition=overview or None,
        event_date=start,
        start_time=start,
        series_format=f"BO{best_of}" if best_of else None,
        maps_won=maps_won,
        current_map=current_map,
        map_finished=False,
        extra={"overview": overview, "winner": winner},
        provenance=LiveDataProvenance(
            provider=_PROVIDER,
            event_identifier=event_id[:180],
            source_locator=_API,
            fetched_at=now,
            source_timestamp=now,
            reliability="structured_wiki",
        ),
    )


def _field(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _aliases(name: str) -> tuple[str, ...]:
    stripped = _PAREN_RE.sub(" ", name).strip()
    if stripped and stripped != name:
        return (stripped,)
    return ()


def _best_of(value: str) -> int | None:
    text = value.upper().replace("BO", "").strip()
    try:
        parsed = int(text)
    except ValueError:
        return None
    if parsed in {1, 3, 5, 7}:
        return parsed
    return None


def _score(a: str, b: str) -> tuple[int, int] | None:
    if a == "" and b == "":
        return None
    try:
        return int(a), int(b)
    except ValueError:
        return None


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(value[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return parsed
    return None
