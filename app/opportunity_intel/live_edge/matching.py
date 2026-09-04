"""Event matching: never match on a single team name."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta

from app.opportunity_intel.live_edge.classify import ACTIONABLE_LIVE_CATEGORIES
from app.opportunity_intel.live_edge.models import (
    DiscoveredMarket,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
    MarketKind,
    MatchResult,
    MatchStatus,
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_GENERIC_TOKENS = frozenset(
    {
        "team",
        "esports",
        "gaming",
        "academy",
        "challengers",
        "club",
        "united",
        "city",
        "sport",
        "sports",
        "de",
        "la",
        "el",
    }
)
_HONORIFIC_TOKENS = frozenset(
    {
        "fc",
        "cf",
        "cd",
        "sc",
        "ac",
        "afc",
        "club",
        "de",
        "la",
        "el",
        "the",
        "sd",
        "ud",
        "rc",
        "ssc",
    }
)
_SPORT_SYNONYMS: dict[str, frozenset[str]] = {
    "soccer": frozenset({"soccer", "football", "football soccer"}),
    "football": frozenset({"soccer", "football", "football soccer"}),
    "football soccer": frozenset({"soccer", "football", "football soccer"}),
    "basketball": frozenset({"basketball", "nba", "wnba"}),
    "baseball": frozenset({"baseball", "mlb"}),
    "tennis": frozenset({"tennis"}),
    "league of legends": frozenset({"league of legends", "lol"}),
    "counter strike 2": frozenset({"counter strike 2", "counter strike", "cs2", "csgo"}),
    "dota 2": frozenset({"dota 2", "dota"}),
}
_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"psg", "paris saint germain", "paris sg"}),
    frozenset({"manchester united", "man united", "man utd", "manchester utd"}),
    frozenset({"manchester city", "man city", "mcfc"}),
    frozenset({"tottenham", "tottenham hotspur", "spurs"}),
    frozenset({"inter milan", "internazionale", "inter"}),
    frozenset({"atletico madrid", "atletico", "atleti"}),
    frozenset({"natus vincere", "navi", "na vi"}),
    frozenset({"team liquid", "liquid", "tl"}),
    frozenset({"g2 esports", "g2"}),
    frozenset({"lyon", "olympique lyonnais", "ol"}),
    frozenset({"barcelona", "fc barcelona", "barca"}),
    frozenset({"bayern", "bayern munich", "fc bayern"}),
    frozenset({"zero tenacity", "z10"}),
    frozenset({"team spirit", "team spirit academy"}),
)

_LONG_HORIZON_RE = re.compile(
    r"\b(?:world series|mvp|cy young|election|nomination|president|"
    r"championship series|(?:al|nl)\s+(?:east|west|central)\b|"
    r"award\b|regular season|win the 20\d{2}\b|"
    r"s[eé]rie\b|liga\s+\d\b|lfpb\b)",
    re.IGNORECASE,
)
_NEAR_END_SECONDS = 6 * 3600


def normalize_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = _PUNCT_RE.sub(" ", folded.lower())
    return " ".join(cleaned.split())


def live_name_index(
    snapshots: tuple[LiveStateSnapshot, ...],
) -> dict[str, list[LiveStateSnapshot]]:
    """Token → snapshots, so evaluation order can skip unrelated markets."""
    index: dict[str, list[LiveStateSnapshot]] = {}
    for snapshot in snapshots:
        names = list(snapshot.participants) + list(snapshot.aliases)
        keys: set[str] = set()
        for name in names:
            keys.update(_alias_keys(name))
            for token in _canonical_tokens(name):
                if len(token) >= 4 and token not in _GENERIC_TOKENS:
                    keys.add(token)
        for key in keys:
            index.setdefault(key, []).append(snapshot)
    return index


def identity_hits_live_index(
    identity: MarketIdentity, index: dict[str, list[LiveStateSnapshot]]
) -> bool:
    if not index or not identity.participants:
        return False
    for part in identity.participants:
        if any(key in index for key in _alias_keys(part)):
            return True
        for token in _canonical_tokens(part):
            if len(token) >= 4 and token not in _GENERIC_TOKENS and token in index:
                return True
    return False


def match_event(
    market: DiscoveredMarket,
    identity: MarketIdentity,
    candidates: tuple[LiveStateSnapshot, ...],
    *,
    now: datetime,
) -> MatchResult:
    if identity.kind in {MarketKind.MAP_WINNER, MarketKind.GAME_WINNER}:
        return match_map_market(market, identity, candidates, now=now)
    return _match_generic(market, identity, candidates, now=now, require_current_map=False)


def match_map_market(
    market: DiscoveredMarket,
    identity: MarketIdentity,
    candidates: tuple[LiveStateSnapshot, ...],
    *,
    now: datetime,
) -> MatchResult:
    result = _match_generic(
        market, identity, candidates, now=now, require_current_map=True
    )
    if result.status not in {MatchStatus.VERIFIED_MATCH, MatchStatus.PROBABLE_MATCH}:
        return result
    wanted = identity.map_number or identity.game_number
    matched = next(
        (item for item in candidates if item.provenance and item.provenance.event_identifier == result.event_identifier),
        None,
    )
    if matched is None:
        return MatchResult(MatchStatus.NO_MATCH, None, ("map candidate lost after match",))
    if wanted is None:
        return MatchResult(
            MatchStatus.AMBIGUOUS,
            result.event_identifier,
            ("map/game number missing from market title",),
        )
    if matched.map_finished and matched.current_map is not None and matched.current_map < wanted:
        return MatchResult(
            MatchStatus.NO_MATCH,
            result.event_identifier,
            ("finished earlier map must not bind a later map market",),
        )
    if matched.current_map is not None and matched.current_map != wanted:
        return MatchResult(
            MatchStatus.NO_MATCH,
            result.event_identifier,
            (
                f"market is map/game {wanted}, live state is map/game "
                f"{matched.current_map}",
            ),
        )
    return result


def _match_generic(
    market: DiscoveredMarket,
    identity: MarketIdentity,
    candidates: tuple[LiveStateSnapshot, ...],
    *,
    now: datetime,
    require_current_map: bool,
) -> MatchResult:
    if not identity.participants:
        return MatchResult(
            MatchStatus.NO_MATCH,
            None,
            ("недостатньо імен учасників для зіставлення",),
        )
    if len(identity.participants) == 1:
        return _match_unique_named_participant(
            market,
            identity,
            candidates,
            now=now,
            require_current_map=require_current_map,
        )
    wanted = [normalize_name(part) for part in identity.participants]
    scored: list[tuple[int, LiveStateSnapshot, tuple[str, ...]]] = []
    for snapshot in candidates:
        score, reasons = _score_candidate(
            wanted, identity, snapshot, now=now, require_current_map=require_current_map
        )
        if score > 0:
            scored.append((score, snapshot, reasons))
    if not scored:
        return MatchResult(MatchStatus.NO_MATCH, None, ("жодна live-подія не зіставлена",))
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best, reasons = scored[0]
    ties = [item for item in scored if item[0] == best_score]
    if len(ties) > 1:
        return MatchResult(
            MatchStatus.AMBIGUOUS,
            None,
            ("кілька подій мають однаковий рівень збігу",),
        )
    event_id = (
        best.provenance.event_identifier if best.provenance is not None else None
    )
    if best_score >= 8:
        return MatchResult(MatchStatus.VERIFIED_MATCH, event_id, reasons)
    if best_score >= 5:
        return MatchResult(MatchStatus.PROBABLE_MATCH, event_id, reasons)
    return MatchResult(MatchStatus.AMBIGUOUS, event_id, reasons)


def _score_candidate(
    wanted: list[str],
    identity: MarketIdentity,
    snapshot: LiveStateSnapshot,
    *,
    now: datetime,
    require_current_map: bool,
) -> tuple[int, tuple[str, ...]]:
    names = [normalize_name(part) for part in snapshot.participants]
    aliases = [normalize_name(part) for part in snapshot.aliases]
    pool = names + aliases
    hits = 0
    for team in wanted:
        if any(_names_match(team, name) for name in pool):
            hits += 1
    if hits < 2:
        return 0, ("збіг лише за одним учасником відхилено",)
    score = 4 + hits
    reasons = [f"збіглися {hits} учасники"]
    if identity.sport_label and snapshot.sport:
        if _sports_compatible(identity.sport_label, snapshot.sport):
            score += 2
            reasons.append("вид спорту збігається")
        else:
            return 0, ("вид спорту не збігається",)
    if identity.competition and snapshot.competition:
        if normalize_name(identity.competition) in normalize_name(snapshot.competition) or (
            normalize_name(snapshot.competition) in normalize_name(identity.competition)
        ):
            score += 1
            reasons.append("турнір збігається")
    event_time = snapshot.start_time or snapshot.event_date
    if event_time is not None:
        delta = abs((event_time - now).total_seconds())
        if delta <= timedelta(hours=12).total_seconds():
            score += 2
            reasons.append("час події в допустимому вікні")
        elif delta > timedelta(days=2).total_seconds():
            score -= 3
            reasons.append("дата події далеко від поточної")
    if snapshot.series_format:
        score += 1
        reasons.append("відомий формат серії")
    if require_current_map and snapshot.current_map is None:
        score -= 4
        reasons.append("немає номера поточної карти")
    return score, tuple(reasons)


def _match_unique_named_participant(
    market: DiscoveredMarket,
    identity: MarketIdentity,
    candidates: tuple[LiveStateSnapshot, ...],
    *,
    now: datetime,
    require_current_map: bool,
) -> MatchResult:
    """One named side may bind only when exactly one current same-sport live event contains that distinctive name."""
    if identity.category not in ACTIONABLE_LIVE_CATEGORIES:
        return MatchResult(
            MatchStatus.NO_MATCH,
            None,
            ("недостатньо імен учасників для зіставлення",),
        )
    if identity.category == LiveEdgeCategory.WEATHER:
        return MatchResult(
            MatchStatus.NO_MATCH,
            None,
            ("недостатньо імен учасників для зіставлення",),
        )
    if not identity.sport_label:
        return MatchResult(
            MatchStatus.NO_MATCH,
            None,
            ("недостатньо імен учасників для зіставлення",),
        )
    if not _market_is_current(market, now):
        return MatchResult(
            MatchStatus.NO_MATCH,
            None,
            ("ринок не live/imminent — одноіменне зіставлення заборонено",),
        )
    if _LONG_HORIZON_RE.search(market.question or ""):
        return MatchResult(
            MatchStatus.NO_MATCH,
            None,
            ("сезонний/нагородний ринок не зіставляється з поточною live-подією",),
        )
    named = identity.participants[0]
    if not _distinctive_participant(named):
        return MatchResult(
            MatchStatus.NO_MATCH,
            None,
            ("недостатньо імен учасників для зіставлення",),
        )
    hits: list[LiveStateSnapshot] = []
    for snapshot in candidates:
        pool = list(snapshot.participants) + list(snapshot.aliases)
        if not any(_strict_names_match(named, item) for item in pool):
            continue
        if not _sports_compatible(identity.sport_label, snapshot.sport):
            continue
        hits.append(snapshot)
    if not hits:
        return MatchResult(MatchStatus.NO_MATCH, None, ("жодна live-подія не зіставлена",))
    if len(hits) > 1:
        return MatchResult(
            MatchStatus.AMBIGUOUS,
            None,
            ("кілька live-подій містять те саме ім'я — fail closed",),
        )
    best = hits[0]
    if require_current_map and best.current_map is None:
        return MatchResult(
            MatchStatus.NO_MATCH,
            best.provenance.event_identifier if best.provenance else None,
            ("немає номера поточної карти",),
        )
    event_id = best.provenance.event_identifier if best.provenance is not None else None
    return MatchResult(
        MatchStatus.PROBABLE_MATCH,
        event_id,
        (
            "унікальна live-подія для названого учасника",
            "PROBABLE_MATCH — другий учасник взято з провайдера, не VERIFIED",
        ),
    )


def _market_is_current(market: DiscoveredMarket, now: datetime) -> bool:
    if market.live is True:
        return True
    if (
        market.start_at is not None
        and market.end_at is not None
        and market.start_at <= now <= market.end_at
    ):
        return True
    if market.end_at is not None:
        remaining = (market.end_at - now).total_seconds()
        if 0 <= remaining <= _NEAR_END_SECONDS:
            return True
    return False


def _strict_names_match(team: str, name: str) -> bool:
    """Unique-bind only: exact/canonical/alias. No token-subset of a longer club name."""
    if not team or not name:
        return False
    left = normalize_name(team)
    right = normalize_name(name)
    if not left or not right:
        return False
    if left == right:
        return True
    left_canon = _canonical_name(team)
    right_canon = _canonical_name(name)
    if left_canon and right_canon and left_canon == right_canon:
        return True
    return bool(_alias_keys(team) & _alias_keys(name))


def _distinctive_participant(value: str) -> bool:
    tokens = [
        token
        for token in _canonical_tokens(value)
        if len(token) >= 4 and token not in _GENERIC_TOKENS
    ]
    if tokens:
        return True
    compact = _canonical_name(value)
    return len(compact) >= 3 and compact not in _GENERIC_TOKENS


def align_snapshot_to_market(
    snapshot: LiveStateSnapshot, identity: MarketIdentity
) -> LiveStateSnapshot:
    """Index 0 is the market's first named participant. Pairwise stats are swapped if needed."""
    if not identity.participants:
        return snapshot
    wanted = normalize_name(identity.participants[0])
    names = [normalize_name(part) for part in snapshot.participants]
    if len(names) < 2:
        return snapshot
    if _names_match(wanted, names[0]):
        return snapshot
    if not _names_match(wanted, names[1]):
        return snapshot
    extra = dict(snapshot.extra)
    if extra.get("home_score") is not None and extra.get("away_score") is not None:
        extra["home_score"], extra["away_score"] = extra["away_score"], extra["home_score"]
    return LiveStateSnapshot(
        sport=snapshot.sport,
        status=snapshot.status,
        participants=(snapshot.participants[1], snapshot.participants[0]),
        aliases=snapshot.aliases,
        competition=snapshot.competition,
        event_date=snapshot.event_date,
        start_time=snapshot.start_time,
        series_format=snapshot.series_format,
        maps_won=_swap_pair(snapshot.maps_won),
        current_map=snapshot.current_map,
        map_finished=snapshot.map_finished,
        kills=_swap_pair(snapshot.kills),
        towers=_swap_pair(snapshot.towers),
        inhibitors=_swap_pair(snapshot.inhibitors),
        dragons=snapshot.dragons,
        barons=snapshot.barons,
        gold_difference=(
            None if snapshot.gold_difference is None else -snapshot.gold_difference
        ),
        game_time_seconds=snapshot.game_time_seconds,
        round_score=_swap_pair(snapshot.round_score),
        side=snapshot.side,
        match_point=snapshot.match_point,
        overtime=snapshot.overtime,
        net_worth_difference=(
            None
            if snapshot.net_worth_difference is None
            else -snapshot.net_worth_difference
        ),
        barracks=_swap_pair(snapshot.barracks),
        sets=_swap_pair(snapshot.sets),
        games=_swap_pair(snapshot.games),
        current_server=snapshot.current_server,
        tennis_point=snapshot.tennis_point,
        score=_swap_pair(snapshot.score),
        period=snapshot.period,
        remaining_seconds=snapshot.remaining_seconds,
        inning=snapshot.inning,
        top_bottom=snapshot.top_bottom,
        outs=snapshot.outs,
        runners=snapshot.runners,
        minute=snapshot.minute,
        red_cards=_swap_pair(snapshot.red_cards),
        extra=extra,
        provenance=snapshot.provenance,
    )


def _swap_pair(value: tuple[int, int] | None) -> tuple[int, int] | None:
    if value is None:
        return None
    return (value[1], value[0])


def _sports_compatible(left: str, right: str) -> bool:
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    left_set = _SPORT_SYNONYMS.get(a, frozenset({a}))
    right_set = _SPORT_SYNONYMS.get(b, frozenset({b}))
    return bool(left_set & right_set)


def _canonical_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in normalize_name(value).split()
        if token and token not in _HONORIFIC_TOKENS
    )


def _canonical_name(value: str) -> str:
    return " ".join(_canonical_tokens(value))


def _alias_keys(value: str) -> set[str]:
    canonical = _canonical_name(value)
    keys = {canonical, normalize_name(value)}
    for group in _ALIAS_GROUPS:
        if canonical in group or normalize_name(value) in group:
            keys.update(group)
    return {item for item in keys if item}


def _names_match(team: str, name: str) -> bool:
    if not team or not name:
        return False
    left = normalize_name(team)
    right = normalize_name(name)
    if not left or not right:
        return False
    if left == right:
        return True
    left_canon = _canonical_name(team)
    right_canon = _canonical_name(name)
    if left_canon and right_canon and left_canon == right_canon:
        return True
    if _alias_keys(team) & _alias_keys(name):
        return True
    left_tokens = [
        token
        for token in _canonical_tokens(team)
        if len(token) >= 4 and token not in _GENERIC_TOKENS
    ]
    right_tokens = [
        token
        for token in _canonical_tokens(name)
        if len(token) >= 4 and token not in _GENERIC_TOKENS
    ]
    if not left_tokens or not right_tokens:
        return False
    if set(left_tokens) <= set(right_tokens) or set(right_tokens) <= set(left_tokens):
        return True
    if _distinctive_token_prefix_match(left_tokens, right_tokens):
        return True
    return False


def _distinctive_token_prefix_match(left: list[str], right: list[str]) -> bool:
    """lyon ↔ lyonnais. 3-letter codes never match by prefix."""
    for a in left:
        for b in right:
            if a == b:
                return True
            if len(a) >= 4 and len(b) >= 6 and b.startswith(a):
                return True
            if len(b) >= 4 and len(a) >= 6 and a.startswith(b):
                return True
    return False
