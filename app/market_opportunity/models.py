"""Unified paper-only Opportunity models for Market Opportunity Intelligence."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum


class OpportunitySide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


class OpportunityStatus(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    SCORED = "scored"
    PUBLISHED = "published"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    MEASURED = "measured"
    CALIBRATED = "calibrated"


class ConfidenceBand(StrEnum):
    """Ordinal completeness/history band — not a predicted win-rate percent."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class OpportunityFamily(StrEnum):
    EVENT_DRIVEN = "event_driven"
    MACRO_CROSS_MARKET = "macro_cross_market"
    TREND_MOMENTUM = "trend_momentum"
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    COMMODITY_SUPPLY_DEMAND = "commodity_supply_demand"
    EARNINGS_COMPANY = "earnings_company"
    PRICE_LIQUIDITY_ANOMALY = "price_liquidity_anomaly"


class AssetClass(StrEnum):
    COMMODITY = "commodity"
    EQUITY = "equity"
    INDEX = "index"
    CRYPTO = "crypto"
    FX = "fx"
    RATES = "rates"
    FUTURES = "futures"
    OTHER = "other"


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must have a UTC offset")
    return value.astimezone(timezone.utc)


def _decimal(
    value: Decimal,
    field_name: str,
    *,
    minimum: Decimal | None = None,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def _identifier(value: str, field_name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


@dataclass(frozen=True)
class InstrumentRef:
    """Explicit instrument identity — bare names like 'Gold' are rejected."""

    asset_class: AssetClass
    symbol: str
    source_id: str
    venue: str
    market_id: str
    display_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.asset_class, AssetClass):
            raise TypeError("asset_class must be AssetClass")
        _identifier(self.symbol, "symbol", maximum=128)
        _identifier(self.source_id, "source_id", maximum=128)
        _identifier(self.venue, "venue", maximum=128)
        _identifier(self.market_id, "market_id", maximum=256)
        if self.display_name:
            _identifier(self.display_name, "display_name", maximum=256)
        # Ambiguous commodity nicknames without market identity are invalid.
        bare = self.symbol.strip().lower()
        if bare in {"gold", "silver", "oil", "crude", "wti", "natgas", "natural gas"}:
            if self.market_id.strip().lower() in {"", bare, self.symbol.strip().lower()}:
                raise ValueError(
                    "ambiguous instrument: require explicit venue and market_id "
                    f"(got symbol={self.symbol!r} market_id={self.market_id!r})"
                )


@dataclass(frozen=True)
class Provenance:
    """Immutable source provenance for a market-data fact."""

    source_id: str
    fetched_at: datetime
    source_event_at: datetime | None
    series_kind: str
    note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _identifier(self.series_kind, "series_kind", maximum=64)
        object.__setattr__(self, "fetched_at", _utc(self.fetched_at, "fetched_at"))
        if self.source_event_at is not None:
            object.__setattr__(
                self, "source_event_at", _utc(self.source_event_at, "source_event_at")
            )
        if self.note:
            _identifier(self.note, "note", maximum=500)


@dataclass(frozen=True)
class EvidenceRef:
    """One evidence bullet tied to provenance."""

    evidence_id: str
    summary: str
    provenance: Provenance
    is_catalyst: bool = False

    def __post_init__(self) -> None:
        _identifier(self.evidence_id, "evidence_id")
        _identifier(self.summary, "summary", maximum=1000)
        if not isinstance(self.provenance, Provenance):
            raise TypeError("provenance must be Provenance")


@dataclass(frozen=True)
class PriceLevel:
    label: str
    price: Decimal
    provenance: Provenance

    def __post_init__(self) -> None:
        _identifier(self.label, "label", maximum=64)
        _decimal(self.price, "price")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("provenance must be Provenance")


@dataclass(frozen=True)
class OpportunityOutcome:
    """Paper outcome stub — populated by later versions; nullable metrics OK."""

    opportunity_uid: str
    entry_reached: bool | None = None
    entry_missed: bool | None = None
    targets_reached: tuple[bool, ...] = ()
    invalidation_reached: bool | None = None
    mfe: Decimal | None = None
    mae: Decimal | None = None
    return_1d: Decimal | None = None
    return_5d: Decimal | None = None
    return_20d: Decimal | None = None
    measured_at: datetime | None = None

    def __post_init__(self) -> None:
        _identifier(self.opportunity_uid, "opportunity_uid")
        for name in ("mfe", "mae", "return_1d", "return_5d", "return_20d"):
            value = getattr(self, name)
            if value is not None:
                _decimal(value, name)
        if self.measured_at is not None:
            object.__setattr__(
                self, "measured_at", _utc(self.measured_at, "measured_at")
            )


@dataclass(frozen=True)
class Opportunity:
    """Canonical paper-only market opportunity idea."""

    opportunity_uid: str
    family: OpportunityFamily
    opportunity_type: str
    side: OpportunitySide
    instrument: InstrumentRef
    status: OpportunityStatus
    confidence: ConfidenceBand
    verified_price: PriceLevel
    entry_zone_low: Decimal
    entry_zone_high: Decimal
    targets: tuple[PriceLevel, ...]
    invalidation: PriceLevel
    time_horizon: str
    reward_risk: Decimal
    evidence: tuple[EvidenceRef, ...]
    counterarguments: tuple[str, ...]
    paper_only: bool
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    outcome: OpportunityOutcome | None = None

    def __post_init__(self) -> None:
        _identifier(self.opportunity_uid, "opportunity_uid")
        _identifier(self.opportunity_type, "opportunity_type", maximum=128)
        _identifier(self.time_horizon, "time_horizon", maximum=64)
        if not isinstance(self.family, OpportunityFamily):
            raise TypeError("family must be OpportunityFamily")
        if not isinstance(self.side, OpportunitySide):
            raise TypeError("side must be OpportunitySide")
        if not isinstance(self.status, OpportunityStatus):
            raise TypeError("status must be OpportunityStatus")
        if not isinstance(self.confidence, ConfidenceBand):
            raise TypeError("confidence must be ConfidenceBand")
        if not isinstance(self.instrument, InstrumentRef):
            raise TypeError("instrument must be InstrumentRef")
        if not self.paper_only:
            raise ValueError("paper_only must be True")
        _decimal(self.entry_zone_low, "entry_zone_low")
        _decimal(self.entry_zone_high, "entry_zone_high")
        if self.entry_zone_low > self.entry_zone_high:
            raise ValueError("entry_zone_low must be <= entry_zone_high")
        _decimal(self.reward_risk, "reward_risk", minimum=Decimal("0"))
        if not self.targets:
            raise ValueError("targets must not be empty")
        for item in self.counterarguments:
            _identifier(item, "counterargument", maximum=1000)
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
