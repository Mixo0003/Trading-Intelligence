"""V4.3 crypto paper opportunity domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ScanReject:
    code: str
    message: str
    symbol: str | None = None
    buy_venue: str | None = None
    sell_venue: str | None = None


@dataclass(frozen=True)
class PaperNetOpportunity:
    """Cross-venue paper net opportunity (public books + verified fees)."""

    opportunity_key: str
    opportunity_uid: str
    pair: str
    product_type: str
    buy_venue: str
    sell_venue: str
    buy_ask: float
    sell_bid: float
    buy_vwap: float
    sell_vwap: float
    gross_spread_usd: float
    buy_fee_usd: float
    sell_fee_usd: float
    trading_fees_usd: float
    estimated_slippage: float
    paper_allocation_usd: Decimal
    executable_base: float
    executable_liquidity_usd: float
    net_profit_usd: float
    roi: float
    signal_age_seconds: float
    checked_at: datetime
    direction: str = "buy_low_sell_high"


@dataclass(frozen=True)
class ScanReport:
    enabled: bool
    opportunities: tuple[PaperNetOpportunity, ...]
    rejects: tuple[ScanReject, ...]
    diagnostics: tuple[str, ...] = ()
