"""Copy-trading intelligence domain models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TradeEventType = Literal["open", "increase", "reduce", "close"]
CopyAction = Literal["COPY", "WATCH", "SKIP"]


@dataclass(frozen=True)
class PositionSnapshot:
    wallet: str
    condition_id: str
    asset: str | None
    outcome: str | None
    size: float
    avg_price: float | None
    cur_price: float | None
    title: str | None
    slug: str | None
    event_slug: str | None


@dataclass(frozen=True)
class TradeEvent:
    wallet: str
    event_type: TradeEventType
    condition_id: str | None
    asset: str | None
    outcome: str | None
    side: str | None
    price: float | None
    size: float | None
    tx_hash: str | None
    ts: int | None
    title: str | None
    slug: str | None
    dedup_key: str
    observed_at: int | None = None
    event_uid: str | None = None
    is_baseline: bool = False


@dataclass(frozen=True)
class TraderMetrics:
    wallet: str
    trade_count: int
    resolved_count: int
    win_rate: float
    realized_pnl: float
    unrealized_pnl: float
    avg_entry_price: float
    avg_holding_hours: float
    max_drawdown: float
    category_concentration: float
    recent_pnl_30d: float
    lifetime_pnl: float
    wash_score: float
    eligible: bool
    ineligible_reason: str | None


@dataclass(frozen=True)
class MarketQuote:
    token_id: str
    midpoint: float | None
    best_bid: float | None
    best_ask: float | None
    bid_depth_usd: float
    ask_depth_usd: float


@dataclass(frozen=True)
class CopyRecommendation:
    wallet: str
    trader_label: str
    market_title: str
    market_slug: str | None
    outcome: str | None
    event_type: TradeEventType
    entry_price: float | None
    current_price: float | None
    price_drift: float | None
    slippage_est: float | None
    trader_score: float
    max_alloc: float
    action: CopyAction
    reason: str


DiscoverySource = Literal["active", "profitable", "category", "rising"]
DiscoveryDecision = Literal["FOLLOW", "WATCH", "AVOID"]
DataConfidence = Literal["Low", "Medium", "High"]
RiskLevel = Literal["Low", "Medium", "High"]
AlertDecisions = Literal["FOLLOW_ONLY", "FOLLOW_AND_WATCH"]
SettingsProfileName = Literal["Conservative", "Balanced", "Exploratory"]


@dataclass(frozen=True)
class DiscoveryCandidate:
    wallet: str
    label: str | None
    sources: tuple[DiscoverySource, ...]
    leaderboard_pnl: float | None
    leaderboard_vol: float | None
    category: str | None


@dataclass(frozen=True)
class DiscoveryMetrics:
    wallet: str
    label: str | None
    realized_pnl: float | None
    roi: float | None
    resolved_count: int
    active_days: int | None
    win_rate: float | None
    avg_position_size: float | None
    median_position_size: float | None
    max_drawdown: float | None
    recent_pnl_30d: float | None
    lifetime_pnl: float | None
    main_categories: tuple[str, ...]
    category_concentration: float | None
    market_concentration: float | None
    single_win_contribution: float | None
    liquidity_quality: float | None
    wash_score: float
    incomplete_metrics: tuple[str, ...]
    profile_url: str


@dataclass(frozen=True)
class DiscoveryScore:
    wallet: str
    label: str | None
    performance_score: float
    risk_score: float
    data_confidence: DataConfidence
    final_score: float
    risk_level: RiskLevel
    decision: DiscoveryDecision
    reason: str
    metrics: DiscoveryMetrics
    rejected: bool
    reject_reason: str | None


@dataclass(frozen=True)
class CopyUserSettings:
    telegram_id: int
    profile: SettingsProfileName
    min_resolved_markets: int
    min_active_days: int
    max_drawdown_pct: float
    max_single_win_contribution: float
    max_category_concentration: float
    min_liquidity_usd: float
    poll_interval_seconds: int
    alert_decisions: AlertDecisions
