"""Typed models for Live Edge Opportunities. No live-data invention."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


class LiveEdgeSignalType(StrEnum):
    LIVE_MAP_EDGE = "LIVE_MAP_EDGE"
    LIVE_MATCH_EDGE = "LIVE_MATCH_EDGE"
    NEAR_RESOLUTION_EDGE = "NEAR_RESOLUTION_EDGE"
    LIVE_GENERAL_EDGE = "LIVE_GENERAL_EDGE"
    WAIT_CANDIDATE = "WAIT_CANDIDATE"
    LIVE_CANDIDATE = "LIVE_CANDIDATE"


class LiveEdgeDecision(StrEnum):
    BUY_NOW = "BUY_NOW"
    WAIT = "WAIT"
    WATCH = "WATCH"
    TOO_LATE = "TOO_LATE"
    SKIP = "SKIP"


DECISION_LABELS_UK: dict[LiveEdgeDecision, str] = {
    LiveEdgeDecision.BUY_NOW: "ЗАХОДИТИ",
    LiveEdgeDecision.WAIT: "ЧЕКАТИ",
    LiveEdgeDecision.WATCH: "ПОКИ СПОСТЕРІГАЄМО",
    LiveEdgeDecision.TOO_LATE: "НЕ ЗАХОДИТИ",
    LiveEdgeDecision.SKIP: "ПРОПУСТИТИ",
}


class MatchStatus(StrEnum):
    VERIFIED_MATCH = "VERIFIED_MATCH"
    PROBABLE_MATCH = "PROBABLE_MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    NO_MATCH = "NO_MATCH"


class EvidenceTier(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class LiveEdgeCategory(StrEnum):
    ESPORTS_LOL = "esports_lol"
    ESPORTS_CS2 = "esports_cs2"
    ESPORTS_DOTA = "esports_dota"
    TENNIS = "tennis"
    BASKETBALL = "basketball"
    BASEBALL = "baseball"
    SOCCER = "soccer"
    WEATHER = "weather"
    CRYPTO = "crypto"
    POLITICS = "politics"
    GEOPOLITICS = "geopolitics"
    ECONOMICS = "economics"
    TECHNOLOGY = "technology"
    GENERAL = "general"
    UNSUPPORTED = "unsupported"


class MarketKind(StrEnum):
    SERIES_WINNER = "SERIES_WINNER"
    MAP_WINNER = "MAP_WINNER"
    GAME_WINNER = "GAME_WINNER"
    HANDICAP = "HANDICAP"
    BINARY_EVENT = "BINARY_EVENT"
    UNKNOWN = "UNKNOWN"


class SkipReason(StrEnum):
    FEATURE_OFF = "FEATURE_OFF"
    UNSUPPORTED_CATEGORY = "UNSUPPORTED_CATEGORY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNRELIABLE_SOURCE = "UNRELIABLE_SOURCE"
    LOW_EVIDENCE = "LOW_EVIDENCE"
    FEE_UNAVAILABLE = "FEE_UNAVAILABLE"
    STALE_LIVE_DATA = "STALE_LIVE_DATA"
    STALE_QUOTE = "STALE_QUOTE"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    NO_MATCH = "NO_MATCH"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    NEGATIVE_EV = "NEGATIVE_EV"
    EDGE_TOO_SMALL = "EDGE_TOO_SMALL"
    PRICE_TOO_EXPENSIVE = "PRICE_TOO_EXPENSIVE"
    MARKET_FINISHED = "MARKET_FINISHED"
    UNRESOLVED_CONDITION = "UNRESOLVED_CONDITION"
    AMBIGUOUS_RESOLUTION = "AMBIGUOUS_RESOLUTION"
    STALE_EXTERNAL_EVIDENCE = "STALE_EXTERNAL_EVIDENCE"
    WRONG_MAP = "WRONG_MAP"
    MAP_ALREADY_FINISHED = "MAP_ALREADY_FINISHED"
    INVENTED_PROBABILITY_FORBIDDEN = "INVENTED_PROBABILITY_FORBIDDEN"
    BANKROLL_NOT_EXECUTABLE = "BANKROLL_NOT_EXECUTABLE"
    NEAR_RESOLUTION_NO_EVIDENCE = "NEAR_RESOLUTION_NO_EVIDENCE"
    MATCH_NOT_VERIFIED = "MATCH_NOT_VERIFIED"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must have a UTC offset")
    return value.astimezone(timezone.utc)


def _finite_decimal(value: Decimal, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    return value


@dataclass(frozen=True)
class ProbabilityRange:
    """Inclusive probability range. Edge/EV always use ``low`` (conservative)."""

    low: Decimal
    high: Decimal

    def __post_init__(self) -> None:
        low = _finite_decimal(self.low, "low")
        high = _finite_decimal(self.high, "high")
        if not Decimal("0") <= low <= high <= Decimal("1"):
            raise ValueError("probability range must satisfy 0 <= low <= high <= 1")
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "high", high)

    @property
    def conservative(self) -> Decimal:
        return self.low


@dataclass(frozen=True)
class LiveDataProvenance:
    provider: str
    event_identifier: str
    source_locator: str
    fetched_at: datetime
    source_timestamp: datetime | None
    reliability: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fetched_at", _utc(self.fetched_at, "fetched_at"))
        if self.source_timestamp is not None:
            object.__setattr__(
                self,
                "source_timestamp",
                _utc(self.source_timestamp, "source_timestamp"),
            )
        if not self.provider or not self.event_identifier or not self.source_locator:
            raise ValueError("provenance requires provider, event identifier, locator")

    def age_seconds(self, now: datetime) -> Decimal:
        clock = _utc(now, "now")
        stamp = self.source_timestamp if self.source_timestamp is not None else self.fetched_at
        age = (clock - stamp).total_seconds()
        return Decimal(str(max(0.0, age)))


@dataclass(frozen=True)
class LiveStateSnapshot:
    """Verified live-event state. Missing metrics stay None — never invented."""

    sport: str
    status: str
    participants: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    competition: str | None = None
    event_date: datetime | None = None
    start_time: datetime | None = None
    series_format: str | None = None
    maps_won: tuple[int, int] | None = None
    current_map: int | None = None
    map_finished: bool = False
    kills: tuple[int, int] | None = None
    towers: tuple[int, int] | None = None
    inhibitors: tuple[int, int] | None = None
    dragons: int | None = None
    barons: int | None = None
    gold_difference: int | None = None
    game_time_seconds: int | None = None
    round_score: tuple[int, int] | None = None
    side: str | None = None
    match_point: bool | None = None
    overtime: bool | None = None
    net_worth_difference: int | None = None
    barracks: tuple[int, int] | None = None
    sets: tuple[int, int] | None = None
    games: tuple[int, int] | None = None
    current_server: str | None = None
    tennis_point: str | None = None
    score: tuple[int, int] | None = None
    period: str | None = None
    remaining_seconds: int | None = None
    inning: int | None = None
    top_bottom: str | None = None
    outs: int | None = None
    runners: str | None = None
    minute: int | None = None
    red_cards: tuple[int, int] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    provenance: LiveDataProvenance | None = None


@dataclass(frozen=True)
class DiscoveredMarket:
    event_id: str
    market_id: str
    condition_id: str
    event_slug: str
    question: str
    category_ids: tuple[str, ...]
    tags: tuple[str, ...]
    live: bool | None
    liquidity_usd: Decimal
    volume_24h_usd: Decimal
    end_at: datetime | None
    outcomes: tuple[tuple[str, str], ...]  # (name, asset_id)
    price_hints: tuple[Decimal | None, ...]
    accepting_orders: bool
    order_book_enabled: bool
    event_title: str | None = None
    start_at: datetime | None = None
    description: str | None = None
    market_slug: str | None = None

    def __post_init__(self) -> None:
        if self.end_at is not None:
            object.__setattr__(self, "end_at", _utc(self.end_at, "end_at"))
        if self.start_at is not None:
            object.__setattr__(self, "start_at", _utc(self.start_at, "start_at"))


@dataclass(frozen=True)
class MarketIdentity:
    kind: MarketKind
    category: LiveEdgeCategory
    map_number: int | None = None
    game_number: int | None = None
    participants: tuple[str, ...] = ()
    competition: str | None = None
    sport_label: str | None = None


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    event_identifier: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class WaitTrigger:
    """Concrete entry condition. WAIT without a trigger is forbidden."""

    description_uk: str
    max_yes_ask: Decimal | None = None
    min_edge_pp: Decimal | None = None
    required_map_win: int | None = None
    require_executable_bankroll: bool = False

    def __post_init__(self) -> None:
        if not self.description_uk.strip():
            raise ValueError("WAIT trigger requires a Ukrainian description")
        if (
            self.max_yes_ask is None
            and self.min_edge_pp is None
            and self.required_map_win is None
            and not self.require_executable_bankroll
        ):
            raise ValueError("WAIT trigger must include a measurable condition")


@dataclass(frozen=True)
class ExecutableQuote:
    side: str
    best_ask: Decimal
    vwap: Decimal
    fully_executable: bool
    allocation_usd: Decimal
    slippage: Decimal | None
    book_at: datetime
    quote_age_seconds: Decimal
    visible_ask_depth_usd: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "book_at", _utc(self.book_at, "book_at"))
        if self.side not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")


@dataclass(frozen=True)
class LiveEdgeEvaluation:
    market: DiscoveredMarket
    identity: MarketIdentity
    signal_type: LiveEdgeSignalType
    match: MatchResult
    probability: ProbabilityRange | None
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    skip_reason: SkipReason | None
    live_state: LiveStateSnapshot | None
    provenance: LiveDataProvenance | None
    unresolved_conditions: tuple[str, ...]
    estimated_end_in_seconds: int | None
    estimated_resolution_note: str | None
    evaluated_at: datetime
    live_data_age_seconds: Decimal | None
    near_resolution: bool = False
    evidence_tier: EvidenceTier = EvidenceTier.LOW
    fee_note: str | None = None


@dataclass(frozen=True)
class LiveEdgeDecisionResult:
    evaluation: LiveEdgeEvaluation
    decision: LiveEdgeDecision
    side: str
    quote: ExecutableQuote | None
    edge_pp: Decimal | None
    expected_value_usd: Decimal | None
    expected_roi: Decimal | None
    wait_trigger: WaitTrigger | None
    skip_reason: SkipReason | None
    decision_reasons: tuple[str, ...]
    calculated_at: datetime


@dataclass(frozen=True)
class LiveEdgeSignal:
    signal_uid: str
    market_id: str
    asset_id: str
    question: str
    category: LiveEdgeCategory
    signal_type: LiveEdgeSignalType
    decision: LiveEdgeDecision
    side: str
    result: LiveEdgeDecisionResult
    first_detected_at: datetime
    first_wait_at: datetime | None = None
    first_buy_at: datetime | None = None
    best_executable_price: Decimal | None = None
    best_edge_pp: Decimal | None = None
    best_expected_value_usd: Decimal | None = None
    final_outcome: str | None = None
    missed: bool = False
    missed_reason: str | None = None
