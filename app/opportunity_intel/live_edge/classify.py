"""Category routing and map/series/near-resolution market identity."""

from __future__ import annotations

import re
from decimal import Decimal
from datetime import datetime, timezone

from app.opportunity_intel.live_edge.models import (
    DiscoveredMarket,
    LiveEdgeCategory,
    LiveEdgeSignalType,
    MarketIdentity,
    MarketKind,
)
from app.opportunity_intel.live_edge.policy import (
    NEAR_RESOLUTION_DISCOVERY_PROBABILITY,
    NEAR_RESOLUTION_MAX_SECONDS,
)

_MAP_RE = re.compile(
    r"\b(?:map|game)\s*(?:#\s*)?(\d+)\b|\b(?:first|1st)\s+(?:map|game)\b|"
    r"\b(?:second|2nd)\s+(?:map|game)\b|\b(?:third|3rd)\s+(?:map|game)\b|"
    r"\b(?:fourth|4th)\s+(?:map|game)\b|\b(?:fifth|5th)\s+(?:map|game)\b",
    re.IGNORECASE,
)
_SERIES_RE = re.compile(
    r"\b(?:series|match)\s+winner\b|\bwin the (?:series|match)\b|"
    r"\b(?:to win|will .* win)\b(?!.*\b(?:map|game)\s*\d)",
    re.IGNORECASE,
)
_HANDICAP_RE = re.compile(r"\bhandicap\b|\bmap handicap\b|\b-1\.5\b|\+1\.5\b", re.IGNORECASE)

_ORDINAL = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
}

_VS_RE = re.compile(r"\s+(?:vs\.?|versus)\s+", re.IGNORECASE)
_PROP_RE = re.compile(
    r"\bodd\s*/\s*even\b|\btotal kills\b|\bover\s*/\s*under\b|\bfirst blood\b|"
    r"\bpistol\b|\bkill handicap\b|\btotal maps\b|\bplayer props?\b|"
    r"\bcorrect score\b|\bexact score\b|\bboth teams to score\b|\bends in daytime\b|"
    r"\bslay a dragon\b|\bfirst tower\b|\bmost kills\b|\bend in a draw\b|"
    r"\bdraw at halftime\b|\bleading at halftime\b|\bsecond half draw\b|"
    r"\bexact score:",
    re.IGNORECASE,
)
_MAP_WIN_RE = re.compile(
    r"\b(?:win(?:s|ner)?|to win)\b.{0,24}\b(?:map|game)\s*(?:#\s*)?\d+\b|"
    r"\b(?:map|game)\s*(?:#\s*)?\d+\b.{0,24}\b(?:win(?:s|ner)?|to win)\b|"
    r"\bwill\s+.+\s+win\s+(?:map|game)\b",
    re.IGNORECASE,
)
_EVENT_DATE_RE = re.compile(r"\bon\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
_WILL_WIN_RE = re.compile(
    r"^(?:will\s+)(.+?)\s+win\b",
    re.IGNORECASE,
)
_TO_WIN_RE = re.compile(r"^(.+?)\s+to win\b", re.IGNORECASE)
_YES_NO = frozenset({"yes", "no", "true", "false"})
_GENERIC_SUBJECTS = frozenset(
    {
        "yes",
        "no",
        "who",
        "which",
        "the",
        "this",
        "that",
        "someone",
        "anyone",
        "temperature",
        "the temperature",
        "it",
    }
)

_CATEGORY_HINTS: tuple[tuple[LiveEdgeCategory, tuple[str, ...]], ...] = (
    (
        LiveEdgeCategory.ESPORTS_LOL,
        ("league of legends", " lol ", " lcs ", " lec ", " lck ", " lpl ", "worlds"),
    ),
    (
        LiveEdgeCategory.ESPORTS_CS2,
        ("counter-strike", " cs2 ", "cs:go", "csgo", "pgl major"),
    ),
    (LiveEdgeCategory.ESPORTS_DOTA, ("dota 2", "dota2", "the international")),
    (LiveEdgeCategory.TENNIS, ("tennis", " atp ", " wta ", "roland garros", "wimbledon")),
    (
        LiveEdgeCategory.BASKETBALL,
        (" nba ", "basketball", "ncaa basketball", " wnba "),
    ),
    (LiveEdgeCategory.BASEBALL, (" mlb ", "baseball", "world series")),
    (
        LiveEdgeCategory.SOCCER,
        ("soccer", "football", " epl ", "premier league", " uefa ", " fifa ", "la liga"),
    ),
    (
        LiveEdgeCategory.WEATHER,
        (
            "temperature",
            "weather",
            "highest temperature",
            "lowest temperature",
            "rainfall",
            "snowfall",
        ),
    ),
    (LiveEdgeCategory.CRYPTO, ("bitcoin", "ethereum", " crypto ", " btc ", " eth ")),
    (
        LiveEdgeCategory.POLITICS,
        ("election", "president", "senate", "congress", "vote", "primary"),
    ),
    (
        LiveEdgeCategory.GEOPOLITICS,
        ("war", "invade", "capture", "ceasefire", "russia", "ukraine", "gaza"),
    ),
    (
        LiveEdgeCategory.ECONOMICS,
        (" fed ", " fomc ", "inflation", " cpi ", "interest rate", "unemployment", " gdp "),
    ),
    (
        LiveEdgeCategory.TECHNOLOGY,
        ("apple", "google", "nvidia", "openai", "ai model", "iphone"),
    ),
)


def classify_market(market: DiscoveredMarket) -> MarketIdentity:
    text = f"{market.question} {market.event_title or ''} {' '.join(market.tags)}"
    lower = text.lower()
    category = _category_from_text(lower, market.category_ids)
    kind, map_number, game_number = _kind_from_question(market.question)
    participants = extract_participants(
        market.question,
        market.event_title,
        event_slug=market.event_slug,
        description=market.description,
        outcomes=tuple(name for name, _token in market.outcomes),
    )
    sport_label = _sport_label(category)
    return MarketIdentity(
        kind=kind,
        category=category,
        map_number=map_number,
        game_number=game_number,
        participants=participants,
        competition=_competition_hint(market),
        sport_label=sport_label,
    )


def extract_participants(
    question: str,
    event_title: str | None,
    *,
    event_slug: str | None = None,
    description: str | None = None,
    outcomes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    sources = [
        event_title or "",
        _slug_to_text(event_slug),
        description or "",
        question,
    ]
    for source in sources:
        pair = _pair_from_vs(source)
        if pair is not None:
            return pair
    named_outcomes = [
        name.strip()
        for name in outcomes
        if name.strip() and name.strip().lower() not in _YES_NO
    ]
    if len(named_outcomes) >= 2:
        return (named_outcomes[0], named_outcomes[1])
    for source in (question, event_title or ""):
        subject = _subject_from_will_win(source)
        if subject is not None:
            return (subject,)
    return ()


def event_date_from_text(*parts: str | None) -> datetime | None:
    for part in parts:
        if not part:
            continue
        match = _EVENT_DATE_RE.search(part)
        if match is None:
            continue
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _slug_to_text(slug: str | None) -> str:
    if not slug:
        return ""
    return slug.replace("-", " ").replace("_", " ")


def _subject_from_will_win(source: str) -> str | None:
    text = source.strip()
    if not text:
        return None
    match = _WILL_WIN_RE.search(text) or _TO_WIN_RE.search(text)
    if match is None:
        return None
    subject = match.group(1).strip(" ?!.")
    subject = re.sub(r"^(?:the\s+)?", "", subject, flags=re.IGNORECASE).strip()
    if not subject or subject.lower() in _GENERIC_SUBJECTS:
        return None
    if len(subject) < 3:
        return None
    return subject


def _pair_from_vs(source: str) -> tuple[str, str] | None:
    match = _VS_RE.search(source)
    if match is None:
        return None
    left = source[: match.start()].strip()
    right = source[match.end() :].strip()
    right = re.split(r"\s+[—\-–:]\s+|\s+\(|\s+-", right, maxsplit=1)[0].strip()
    left = re.split(r"\s+[—\-–:]\s+", left)[-1].strip()
    left = re.sub(r"^(?:will|spread|moneyline|winner)\s+", "", left, flags=re.IGNORECASE).strip()
    if left and right and left.lower() != right.lower():
        return (left, right)
    return None


def signal_type_for(
    identity: MarketIdentity,
    market: DiscoveredMarket,
    *,
    now_remaining_seconds: int | None,
    best_price_hint: Decimal | None,
) -> LiveEdgeSignalType:
    if identity.kind in {MarketKind.MAP_WINNER, MarketKind.GAME_WINNER}:
        return LiveEdgeSignalType.LIVE_MAP_EDGE
    if identity.kind == MarketKind.SERIES_WINNER and market.live is True:
        return LiveEdgeSignalType.LIVE_MATCH_EDGE
    if is_near_resolution_candidate(
        remaining_seconds=now_remaining_seconds, price_hint=best_price_hint
    ):
        return LiveEdgeSignalType.NEAR_RESOLUTION_EDGE
    if market.live is True:
        return LiveEdgeSignalType.LIVE_GENERAL_EDGE
    return LiveEdgeSignalType.LIVE_GENERAL_EDGE


def is_near_resolution_candidate(
    *, remaining_seconds: int | None, price_hint: Decimal | None
) -> bool:
    """Discovery threshold only — never an automatic BUY."""
    if remaining_seconds is None or remaining_seconds < 0:
        return False
    if remaining_seconds > NEAR_RESOLUTION_MAX_SECONDS:
        return False
    if price_hint is None or price_hint < NEAR_RESOLUTION_DISCOVERY_PROBABILITY:
        return False
    return True


ACTIONABLE_LIVE_CATEGORIES: frozenset[LiveEdgeCategory] = frozenset(
    {
        LiveEdgeCategory.ESPORTS_LOL,
        LiveEdgeCategory.ESPORTS_CS2,
        LiveEdgeCategory.ESPORTS_DOTA,
        LiveEdgeCategory.TENNIS,
        LiveEdgeCategory.BASKETBALL,
        LiveEdgeCategory.BASEBALL,
        LiveEdgeCategory.SOCCER,
        LiveEdgeCategory.WEATHER,
    }
)

_DEPRIORITIZED_CATEGORIES: frozenset[LiveEdgeCategory] = frozenset(
    {
        LiveEdgeCategory.POLITICS,
        LiveEdgeCategory.GEOPOLITICS,
        LiveEdgeCategory.ECONOMICS,
        LiveEdgeCategory.TECHNOLOGY,
        LiveEdgeCategory.GENERAL,
        LiveEdgeCategory.UNSUPPORTED,
        LiveEdgeCategory.CRYPTO,
    }
)

_FAR_FUTURE_SECONDS = 24 * 3600


def scan_priority(
    identity: MarketIdentity,
    market: DiscoveredMarket,
    *,
    now: datetime | None = None,
) -> int:
    """Lower is higher priority. Politics/general/far-future do not outrank live sports."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    remaining = _remaining_seconds(market, clock)
    near = is_near_resolution_candidate(
        remaining_seconds=remaining, price_hint=_best_hint_for_priority(market)
    )
    actionable = _actionable_for_scan(identity, market)
    liveish = _is_live_or_imminent(market, clock)
    far_future = remaining is not None and remaining > _FAR_FUTURE_SECONDS
    map_kind = identity.kind in {MarketKind.MAP_WINNER, MarketKind.GAME_WINNER}
    series_kind = identity.kind == MarketKind.SERIES_WINNER
    parseable = identity.category == LiveEdgeCategory.WEATHER or len(identity.participants) >= 2
    elapsed = _event_already_elapsed(market, clock)

    if elapsed:
        return 9
    if not parseable and identity.category != LiveEdgeCategory.WEATHER:
        return 9
    if _PROP_RE.search(market.question):
        return 9
    if map_kind and actionable and (liveish or not far_future):
        return 1
    if series_kind and actionable and liveish:
        return 2
    if near and actionable:
        return 3
    if market.live is True and actionable:
        return 4
    if actionable and not far_future:
        return 5
    if market.live is True:
        return 6
    if near:
        return 8
    return 9


def _remaining_seconds(market: DiscoveredMarket, clock: datetime) -> int | None:
    if market.end_at is None:
        return None
    return int((market.end_at.astimezone(timezone.utc) - clock).total_seconds())


def _event_already_elapsed(market: DiscoveredMarket, clock: datetime) -> bool:
    stamped = event_date_from_text(market.question, market.event_title, market.description)
    if stamped is None:
        return False
    return stamped.date() < clock.astimezone(timezone.utc).date()


def _is_live_or_imminent(market: DiscoveredMarket, clock: datetime) -> bool:
    if market.live is True:
        return True
    remaining = _remaining_seconds(market, clock)
    if remaining is not None and 0 <= remaining <= NEAR_RESOLUTION_MAX_SECONDS:
        return True
    if (
        market.start_at is not None
        and market.end_at is not None
        and market.start_at <= clock <= market.end_at
    ):
        return True
    return False


def _actionable_for_scan(identity: MarketIdentity, market: DiscoveredMarket) -> bool:
    if identity.category in _DEPRIORITIZED_CATEGORIES:
        if identity.kind in {MarketKind.MAP_WINNER, MarketKind.GAME_WINNER}:
            return True
        tags = f"{' '.join(market.tags)} {' '.join(market.category_ids)}".lower()
        if "esports" in tags and identity.kind == MarketKind.SERIES_WINNER:
            return True
        return False
    return identity.category in ACTIONABLE_LIVE_CATEGORIES


def _best_hint_for_priority(market: DiscoveredMarket) -> Decimal | None:
    hints = [hint for hint in market.price_hints if hint is not None]
    return max(hints) if hints else None


def _kind_from_question(question: str) -> tuple[MarketKind, int | None, int | None]:
    if _HANDICAP_RE.search(question):
        return MarketKind.HANDICAP, None, None
    if _PROP_RE.search(question):
        return MarketKind.BINARY_EVENT, None, None
    map_match = _MAP_RE.search(question)
    if map_match is not None and _MAP_WIN_RE.search(question):
        number = _map_number(question, map_match)
        lower = question.lower()
        if "game" in lower and "map" not in lower:
            return MarketKind.GAME_WINNER, None, number
        return MarketKind.MAP_WINNER, number, number
    if re.search(r"\bseries winner\b|\bmatch winner\b", question, re.IGNORECASE):
        return MarketKind.SERIES_WINNER, None, None
    if _SERIES_RE.search(question):
        return MarketKind.SERIES_WINNER, None, None
    return MarketKind.BINARY_EVENT, None, None


def _map_number(question: str, match: re.Match[str]) -> int | None:
    if match.group(1):
        return int(match.group(1))
    lower = question.lower()
    for word, number in _ORDINAL.items():
        if re.search(rf"\b{word}\s+(?:map|game)\b", lower):
            return number
    return None


def _category_from_text(
    lower: str, category_ids: tuple[str, ...]
) -> LiveEdgeCategory:
    joined_ids = " ".join(category_ids).lower()
    haystack = f" {lower} {joined_ids} "
    for category, hints in _CATEGORY_HINTS:
        if any(hint in haystack for hint in hints):
            return category
    if "esports" in haystack:
        return LiveEdgeCategory.GENERAL
    if "weather" in joined_ids:
        return LiveEdgeCategory.WEATHER
    if "crypto" in joined_ids:
        return LiveEdgeCategory.CRYPTO
    if "politics" in joined_ids or "elections" in joined_ids:
        return LiveEdgeCategory.POLITICS
    if "finance" in joined_ids or "business" in joined_ids:
        return LiveEdgeCategory.ECONOMICS
    if "tech" in joined_ids:
        return LiveEdgeCategory.TECHNOLOGY
    if "world" in joined_ids:
        return LiveEdgeCategory.GEOPOLITICS
    return LiveEdgeCategory.GENERAL


def _sport_label(category: LiveEdgeCategory) -> str | None:
    return {
        LiveEdgeCategory.ESPORTS_LOL: "League of Legends",
        LiveEdgeCategory.ESPORTS_CS2: "Counter-Strike 2",
        LiveEdgeCategory.ESPORTS_DOTA: "Dota 2",
        LiveEdgeCategory.TENNIS: "Tennis",
        LiveEdgeCategory.BASKETBALL: "Basketball",
        LiveEdgeCategory.BASEBALL: "Baseball",
        LiveEdgeCategory.SOCCER: "Football / Soccer",
        LiveEdgeCategory.WEATHER: "Weather",
    }.get(category)


def _competition_hint(market: DiscoveredMarket) -> str | None:
    if market.event_title and _VS_RE.search(market.event_title) is None:
        return market.event_title
    if market.tags:
        return market.tags[0]
    return None
