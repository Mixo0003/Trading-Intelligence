"""Normalized domain models for Crypto Intelligence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

MarketType = Literal["spot"]
EXCHANGE_IDS = (
    "binance",
    "bybit",
    "okx",
    "bingx",
    "mexc",
    "kucoin",
)


class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNSUPPORTED = "unsupported"
    DISABLED = "disabled"


@dataclass(frozen=True)
class Exchange:
    id: str
    display_name: str


@dataclass(frozen=True)
class Instrument:
    base: str
    quote: str
    market_type: MarketType
    canonical_symbol: str

    @classmethod
    def from_canonical(cls, symbol: str, *, market_type: MarketType = "spot") -> Instrument | None:
        text = (symbol or "").strip().upper().replace("-", "/").replace("_", "/")
        if market_type != "spot":
            return None
        if "/" not in text:
            return None
        base, quote = text.split("/", 1)
        base = base.strip()
        quote = quote.strip()
        if not base or not quote:
            return None
        return cls(
            base=base,
            quote=quote,
            market_type="spot",
            canonical_symbol=f"{base}/{quote}",
        )


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class Ticker:
    exchange_id: str
    instrument: Instrument
    best_bid: float
    best_ask: float
    timestamp_ms: int


@dataclass(frozen=True)
class OrderBookSnapshot:
    exchange_id: str
    instrument: Instrument
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    timestamp_ms: int


@dataclass(frozen=True)
class FeeSchedule:
    exchange_id: str
    market_type: MarketType
    maker_rate: float
    taker_rate: float


@dataclass(frozen=True)
class ExchangeHealth:
    exchange_id: str
    status: HealthStatus
    detail: str
    checked_at_ms: int
    latency_ms: float | None = None


@dataclass(frozen=True)
class SpreadQuote:
    exchange_id: str
    instrument: Instrument
    best_bid: float
    best_ask: float
    spread_pct: float
    timestamp_ms: int
    age_seconds: float
    freshness: Literal["fresh", "stale", "rejected"]


@dataclass(frozen=True)
class OverviewRow:
    """One successful Market Overview quote (V3.0.0b)."""

    exchange_id: str
    instrument: Instrument
    market_type: MarketType
    best_bid: float
    best_ask: float
    midpoint: float
    timestamp_ms: int
    age_seconds: float
    freshness: Literal["fresh"]
    health_status: HealthStatus


@dataclass(frozen=True)
class OverviewReject:
    exchange_id: str | None
    symbol: str | None
    code: str
    message: str


@dataclass(frozen=True)
class OverviewReport:
    enabled: bool
    symbols: tuple[str, ...]
    rows: tuple[OverviewRow, ...]
    rejects: tuple[OverviewReject, ...]


@dataclass(frozen=True)
class ArbitrageCandidate:
    """Reserved for V3.0.0c — never presented as guaranteed profit."""

    buy_exchange_id: str
    sell_exchange_id: str
    instrument: Instrument
    buy_ask: float
    sell_bid: float
    gross_spread_pct: float
    net_spread_pct: float | None
    mode: Literal["pre_funded", "transfer_required"]
    executable: bool
    reject_reason: str | None
