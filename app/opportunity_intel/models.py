"""Typed, payload-free domain models for Market Opportunity Scanner."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.opportunity_intel.settings import OpportunityScannerSettings


class ObservationMode(StrEnum):
    NORMAL = "NORMAL"
    LIVE_STRICT = "LIVE_STRICT"


class LiveMode(StrEnum):
    OFF = "OFF"
    STRICT = "STRICT"


class AlertMode(StrEnum):
    ON_DEMAND_ONLY = "ON_DEMAND_ONLY"
    INSTANT = "INSTANT"


class SignalFamily(StrEnum):
    EXECUTABLE_MOMENTUM = "EXECUTABLE_MOMENTUM"


class MovementDirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class SignalStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"  # legacy alias accepted for reclaim
    SENT = "sent"
    DEAD_LETTER = "dead_letter"
    SUPPRESSED = "suppressed"


class ScannerStatus(StrEnum):
    DISABLED = "DISABLED"
    BASELINING = "BASELINING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class OpportunityReasonCode(StrEnum):
    FAST_5M_MOVE = "FAST_5M_MOVE"
    SUSTAINED_15M_MOVE = "SUSTAINED_15M_MOVE"
    SUSTAINED_1H_MOVE = "SUSTAINED_1H_MOVE"
    HIGH_24H_VOLUME = "HIGH_24H_VOLUME"
    DEEP_EXECUTABLE_BOOK = "DEEP_EXECUTABLE_BOOK"
    LOW_SLIPPAGE = "LOW_SLIPPAGE"
    TIGHT_SPREAD = "TIGHT_SPREAD"
    LIVE_MARKET = "LIVE_MARKET"
    LIVE_FAST_5M_MOVE = "LIVE_FAST_5M_MOVE"
    LIVE_SUSTAINED_15M_MOVE = "LIVE_SUSTAINED_15M_MOVE"
    LIVE_SUSTAINED_1H_MOVE = "LIVE_SUSTAINED_1H_MOVE"
    LIVE_DEEP_EXECUTABLE_BOOK = "LIVE_DEEP_EXECUTABLE_BOOK"
    LIVE_LOW_SLIPPAGE = "LIVE_LOW_SLIPPAGE"


class OpportunityDiagnosticCode(StrEnum):
    DISCOVERY_INCOMPLETE = "DISCOVERY_INCOMPLETE"
    DISCOVERY_IN_PROGRESS = "DISCOVERY_IN_PROGRESS"
    DISCOVERY_SESSION_EXPIRED = "DISCOVERY_SESSION_EXPIRED"
    DISCOVERY_SESSION_INVALID = "DISCOVERY_SESSION_INVALID"
    DISCOVERY_PAGE_FAILED = "DISCOVERY_PAGE_FAILED"
    DISCOVERY_CURSOR_REPEAT = "DISCOVERY_CURSOR_REPEAT"
    DISCOVERY_CURSOR_INVALID = "DISCOVERY_CURSOR_INVALID"
    DISCOVERY_PAGE_CAP = "DISCOVERY_PAGE_CAP"
    DISCOVERY_RESPONSE_INVALID = "DISCOVERY_RESPONSE_INVALID"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    DUPLICATE_MARKET = "DUPLICATE_MARKET"
    DUPLICATE_ASSET = "DUPLICATE_ASSET"
    MARKET_IDENTITY_INVALID = "MARKET_IDENTITY_INVALID"
    MARKET_STATUS_INVALID = "MARKET_STATUS_INVALID"
    OUTCOME_TOKEN_MISMATCH = "OUTCOME_TOKEN_MISMATCH"
    MARKET_TIME_INVALID = "MARKET_TIME_INVALID"
    MARKET_EXPIRED = "MARKET_EXPIRED"
    MARKET_TIME_REMAINING_LOW = "MARKET_TIME_REMAINING_LOW"
    MARKET_NUMERIC_INVALID = "MARKET_NUMERIC_INVALID"
    MARKET_CATEGORY_INVALID = "MARKET_CATEGORY_INVALID"
    LINK_SLUG_INVALID = "LINK_SLUG_INVALID"
    # Retained for compatibility only. Gamma updatedAt age must not emit this.
    METADATA_STALE = "METADATA_STALE"
    MARKET_UNSAFE = "MARKET_UNSAFE"
    IDENTITY_INVALID = "IDENTITY_INVALID"
    HISTORY_REQUEST_FAILED = "HISTORY_REQUEST_FAILED"
    HISTORY_RESPONSE_INVALID = "HISTORY_RESPONSE_INVALID"
    HISTORY_POINT_INVALID = "HISTORY_POINT_INVALID"
    HISTORY_TARGET_MISSING = "HISTORY_TARGET_MISSING"
    HISTORY_LATEST_STALE = "HISTORY_LATEST_STALE"
    HISTORY_MISSING = "HISTORY_MISSING"
    HISTORY_STALE = "HISTORY_STALE"
    HISTORY_BOOK_MISMATCH = "HISTORY_BOOK_MISMATCH"
    BOOK_UNAVAILABLE = "BOOK_UNAVAILABLE"
    BOOK_INVALID = "BOOK_INVALID"
    BOOK_STALE = "BOOK_STALE"
    BOOK_ASSET_MISMATCH = "BOOK_ASSET_MISMATCH"
    BOOK_EMPTY = "BOOK_EMPTY"
    BOOK_ONE_SIDED = "BOOK_ONE_SIDED"
    BOOK_CROSSED = "BOOK_CROSSED"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    SCORE_BELOW_FLOOR = "SCORE_BELOW_FLOOR"
    MIDPOINT_OUT_OF_BOUNDS = "MIDPOINT_OUT_OF_BOUNDS"
    LIQUIDITY_TOO_LOW = "LIQUIDITY_TOO_LOW"
    VOLUME_TOO_LOW = "VOLUME_TOO_LOW"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    MOVEMENT_TRIGGER_MISSING = "MOVEMENT_TRIGGER_MISSING"
    LIVE_DATA_INCONSISTENT = "LIVE_DATA_INCONSISTENT"
    LIVE_HISTORY_STALE = "LIVE_HISTORY_STALE"
    LIVE_BOOK_UNSTABLE = "LIVE_BOOK_UNSTABLE"
    LIVE_CONFIRMATION_FAILED = "LIVE_CONFIRMATION_FAILED"
    BASELINE_RESET_IDENTITY = "BASELINE_RESET_IDENTITY"
    BASELINE_RESET_MODE = "BASELINE_RESET_MODE"
    BASELINE_RESET_VERSION = "BASELINE_RESET_VERSION"
    BASELINE_RESET_STALE = "BASELINE_RESET_STALE"
    STORAGE_FAILURE = "STORAGE_FAILURE"
    CYCLE_DEADLINE_EXCEEDED = "CYCLE_DEADLINE_EXCEEDED"
    CLOB_BUDGET_EXHAUSTED = "CLOB_BUDGET_EXHAUSTED"
    GAMMA_BUDGET_EXHAUSTED = "GAMMA_BUDGET_EXHAUSTED"
    DELIVERY_FAILURE = "DELIVERY_FAILURE"
    SCHEDULER_LAG = "SCHEDULER_LAG"
    ADMIN_GATE_DISABLED = "ADMIN_GATE_DISABLED"
    USER_GATE_DISABLED = "USER_GATE_DISABLED"
    SUBSCRIPTION_DISABLED = "SUBSCRIPTION_DISABLED"
    ALERT_MODE_NOT_INSTANT = "ALERT_MODE_NOT_INSTANT"
    QUIET_HOURS_ACTIVE = "QUIET_HOURS_ACTIVE"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    ALLOCATION_METRIC_MISSING = "ALLOCATION_METRIC_MISSING"
    ALLOCATION_NOT_EXECUTABLE = "ALLOCATION_NOT_EXECUTABLE"
    CATEGORY_NOT_ALLOWED = "CATEGORY_NOT_ALLOWED"
    LIVE_MODE_OFF = "LIVE_MODE_OFF"
    LIVE_MODE_MISMATCH = "LIVE_MODE_MISMATCH"
    SETTINGS_REVISION_CHANGED = "SETTINGS_REVISION_CHANGED"
    AUTHORIZATION_REVOKED = "AUTHORIZATION_REVOKED"
    TELEGRAM_TIMEOUT = "TELEGRAM_TIMEOUT"
    TELEGRAM_RATE_LIMITED = "TELEGRAM_RATE_LIMITED"
    TELEGRAM_NETWORK_ERROR = "TELEGRAM_NETWORK_ERROR"
    TELEGRAM_SERVER_ERROR = "TELEGRAM_SERVER_ERROR"
    TELEGRAM_FORBIDDEN = "TELEGRAM_FORBIDDEN"
    TELEGRAM_CHAT_INVALID = "TELEGRAM_CHAT_INVALID"
    TELEGRAM_BLOCKED = "TELEGRAM_BLOCKED"
    TELEGRAM_RECIPIENT_MALFORMED = "TELEGRAM_RECIPIENT_MALFORMED"
    PAYLOAD_CONTRACT_VIOLATION = "PAYLOAD_CONTRACT_VIOLATION"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


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
    maximum: Decimal | None = None,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be <= {maximum}")
    return value


def _identifier(value: str, field_name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


def _safe_card(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("payload_text must be sanitized Telegram card text")
    for char in value:
        if char != "\n" and unicodedata.category(char).startswith("C"):
            raise ValueError("payload_text contains control characters")
    return value


@dataclass(frozen=True)
class ExecutionMetrics:
    asset_id: str
    observed_at: datetime
    allocation_usd: Decimal
    fully_executable: bool
    execution_vwap: Decimal | None
    slippage: Decimal | None
    quantity: Decimal
    consumed_ask_usd: Decimal

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "asset_id")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        _decimal(self.allocation_usd, "allocation_usd", minimum=Decimal("0.01"))
        _decimal(self.quantity, "quantity", minimum=Decimal("0"))
        _decimal(self.consumed_ask_usd, "consumed_ask_usd", minimum=Decimal("0"))
        if self.execution_vwap is not None:
            _decimal(
                self.execution_vwap,
                "execution_vwap",
                minimum=Decimal("0"),
                maximum=Decimal("1"),
            )
        if self.slippage is not None:
            _decimal(self.slippage, "slippage", minimum=Decimal("-1"))
        if self.fully_executable and (
            self.execution_vwap is None or self.slippage is None
        ):
            raise ValueError("fully executable metrics require VWAP and slippage")


@dataclass(frozen=True)
class CanonicalObservation:
    asset_id: str
    condition_id: str
    event_id: str
    market_id: str
    event_slug: str
    question: str
    outcome: str
    category_ids: tuple[str, ...]
    mode: ObservationMode
    observed_at: datetime
    metadata_at: datetime
    history_at: datetime
    book_at: datetime
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    absolute_spread: Decimal
    relative_spread: Decimal
    liquidity_usd: Decimal
    volume_24h_usd: Decimal
    move_5m: Decimal
    move_15m: Decimal
    move_1h: Decimal
    visible_bid_depth_usd: Decimal
    visible_ask_depth_usd: Decimal
    canonical_execution: ExecutionMetrics

    def __post_init__(self) -> None:
        for name in ("asset_id", "condition_id", "event_id", "market_id"):
            _identifier(getattr(self, name), name)
        _identifier(self.event_slug, "event_slug", maximum=200)
        _identifier(self.question, "question", maximum=500)
        _identifier(self.outcome, "outcome", maximum=120)
        if not self.category_ids:
            raise ValueError("category_ids must not be empty")
        if len(set(self.category_ids)) != len(self.category_ids):
            raise ValueError("category_ids must be unique")
        for name in ("observed_at", "metadata_at", "history_at", "book_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        for name in ("best_bid", "best_ask", "midpoint"):
            _decimal(
                getattr(self, name),
                name,
                minimum=Decimal("0"),
                maximum=Decimal("1"),
            )
        for name in (
            "absolute_spread",
            "relative_spread",
            "liquidity_usd",
            "volume_24h_usd",
            "visible_bid_depth_usd",
            "visible_ask_depth_usd",
        ):
            _decimal(getattr(self, name), name, minimum=Decimal("0"))
        for name in ("move_5m", "move_15m", "move_1h"):
            _decimal(getattr(self, name), name, minimum=Decimal("-1"), maximum=Decimal("1"))
        if self.best_bid >= self.best_ask:
            raise ValueError("observation requires best_bid < best_ask")
        if self.canonical_execution.asset_id != self.asset_id:
            raise ValueError("canonical execution asset mismatch")
        if self.canonical_execution.observed_at != self.observed_at:
            raise ValueError("canonical execution timestamp mismatch")


@dataclass(frozen=True)
class OpportunitySignal:
    opportunity_uid: str
    opportunity_key: str
    asset_id: str
    episode_sequence: int
    material_change_sequence: int
    family: SignalFamily
    direction: MovementDirection
    mode: ObservationMode
    score: Decimal
    reason_codes: tuple[OpportunityReasonCode, ...]
    signal_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    source_observed_at: datetime
    status: SignalStatus = SignalStatus.ACTIVE

    def __post_init__(self) -> None:
        for name in ("opportunity_uid", "opportunity_key", "asset_id"):
            _identifier(getattr(self, name), name)
        if self.episode_sequence < 1 or self.material_change_sequence < 0:
            raise ValueError("signal sequences are invalid")
        _decimal(self.score, "score", minimum=Decimal("0"), maximum=Decimal("100"))
        if not self.reason_codes:
            raise ValueError("reason_codes must not be empty")
        if any(not isinstance(code, OpportunityReasonCode) for code in self.reason_codes):
            raise TypeError("reason_codes must use OpportunityReasonCode")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        for name in (
            "signal_at",
            "first_seen_at",
            "last_seen_at",
            "expires_at",
            "source_observed_at",
        ):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.expires_at <= self.signal_at:
            raise ValueError("expires_at must be after signal_at")


@dataclass(frozen=True)
class OpportunitySubscription:
    telegram_id: int
    enabled: bool
    settings: "OpportunityScannerSettings"

    def __post_init__(self) -> None:
        if not isinstance(self.telegram_id, int) or self.telegram_id <= 0:
            raise ValueError("telegram_id must be a positive integer")
        from app.opportunity_intel.settings import OpportunityScannerSettings

        if not isinstance(self.settings, OpportunityScannerSettings):
            raise TypeError("settings must be OpportunityScannerSettings")


@dataclass(frozen=True)
class OpportunityDelivery:
    telegram_id: int
    opportunity_uid: str
    status: DeliveryStatus
    payload_text: str
    signal_at: datetime
    wave_at: datetime
    settings_revision: int
    claim_id: str | None = None
    claimed_at: datetime | None = None
    claim_expires_at: datetime | None = None
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    last_error_code: OpportunityDiagnosticCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.telegram_id, int) or self.telegram_id <= 0:
            raise ValueError("telegram_id must be a positive integer")
        _identifier(self.opportunity_uid, "opportunity_uid")
        _safe_card(self.payload_text)
        object.__setattr__(self, "signal_at", _utc(self.signal_at, "signal_at"))
        object.__setattr__(self, "wave_at", _utc(self.wave_at, "wave_at"))
        for name in ("claimed_at", "claim_expires_at", "next_attempt_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))
        if self.settings_revision < 0 or self.attempt_count < 0:
            raise ValueError("delivery revision and attempts must be non-negative")
        if self.status == DeliveryStatus.CLAIMED and (
            not self.claim_id
            or self.claimed_at is None
            or self.claim_expires_at is None
        ):
            raise ValueError("claimed delivery requires claim identity and lease")
        if self.claim_id is not None:
            _identifier(self.claim_id, "claim_id")


@dataclass(frozen=True)
class OpportunityBaseline:
    asset_id: str
    mode: ObservationMode
    baseline_version: int
    consecutive_complete_cycles: int
    last_complete_cycle_id: str | None
    last_observed_at: datetime | None
    ready: bool

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "asset_id")
        if self.baseline_version < 1:
            raise ValueError("baseline_version must be positive")
        if self.consecutive_complete_cycles not in (0, 1, 2):
            raise ValueError("baseline cycle count must be 0, 1, or 2")
        if self.last_observed_at is not None:
            object.__setattr__(
                self,
                "last_observed_at",
                _utc(self.last_observed_at, "last_observed_at"),
            )
        if self.ready != (self.consecutive_complete_cycles == 2):
            raise ValueError("ready must match two complete baseline cycles")


@dataclass(frozen=True)
class OpportunityScannerState:
    status: ScannerStatus
    schema_version: int
    baseline_version: int
    score_version: int
    global_baseline_cycles: int
    discovery_complete: bool
    last_complete_discovery_at: datetime | None = None
    last_complete_cycle_at: datetime | None = None
    diagnostic_code: OpportunityDiagnosticCode | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        for name in ("schema_version", "baseline_version", "score_version"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if self.global_baseline_cycles not in (0, 1, 2):
            raise ValueError("global_baseline_cycles must be 0, 1, or 2")
        for name in (
            "last_complete_discovery_at",
            "last_complete_cycle_at",
            "updated_at",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))


def decimal_text(value: Decimal) -> str:
    """Return a stable non-exponent decimal representation for SQLite."""
    _decimal(value, "value")
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"
