"""Balanced defaults, safe bounds, and immutable scanner user settings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.opportunity_intel.models import AlertMode, LiveMode

BALANCED_MINIMUM_SCORE = 65
BALANCED_MINIMUM_LIQUIDITY_USD = Decimal("10000")
BALANCED_MINIMUM_24H_VOLUME_USD = Decimal("25000")
BALANCED_MAXIMUM_RELATIVE_SPREAD = Decimal("0.10")
BALANCED_MAXIMUM_ABSOLUTE_SPREAD = Decimal("0.05")
BALANCED_MAXIMUM_SLIPPAGE = Decimal("0.02")
BALANCED_PAPER_ALLOCATION_USD = Decimal("100")
BALANCED_COOLDOWN_SECONDS = 1800
BALANCED_MINIMUM_TIME_REMAINING_SECONDS = 21600
BALANCED_MAXIMUM_BOOK_AGE_SECONDS = 30

# Deprecated as a Stage A tradeability gate. Gamma market/event updatedAt is
# provenance only — not quote, trade, book, or fetch freshness. Execution
# freshness is enforced via live history/CLOB book timestamps.
NORMAL_METADATA_MAXIMUM_AGE_SECONDS = 600
NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS = 120
NORMAL_HISTORY_5M_TOLERANCE_SECONDS = 120
NORMAL_HISTORY_15M_TOLERANCE_SECONDS = 180
NORMAL_HISTORY_1H_TOLERANCE_SECONDS = 300
NORMAL_HISTORY_MIDPOINT_FLOOR = Decimal("0.02")

STRICT_LIVE_MINIMUM_SCORE = 75
STRICT_LIVE_MINIMUM_LIQUIDITY_USD = Decimal("25000")
STRICT_LIVE_MINIMUM_24H_VOLUME_USD = Decimal("50000")
STRICT_LIVE_MAXIMUM_RELATIVE_SPREAD = Decimal("0.05")
STRICT_LIVE_MAXIMUM_ABSOLUTE_SPREAD = Decimal("0.03")
STRICT_LIVE_MAXIMUM_SLIPPAGE = Decimal("0.01")
STRICT_LIVE_MAXIMUM_BOOK_AGE_SECONDS = 10
STRICT_LIVE_MINIMUM_TIME_REMAINING_SECONDS = 900
# Deprecated as a Stage A tradeability gate (see NORMAL_METADATA note above).
STRICT_LIVE_METADATA_MAXIMUM_AGE_SECONDS = 120
STRICT_LIVE_LATEST_HISTORY_MAXIMUM_AGE_SECONDS = 60
STRICT_LIVE_HISTORY_5M_TOLERANCE_SECONDS = 60
STRICT_LIVE_HISTORY_15M_TOLERANCE_SECONDS = 120
STRICT_LIVE_HISTORY_1H_TOLERANCE_SECONDS = 180
STRICT_LIVE_HISTORY_MIDPOINT_FLOOR = Decimal("0.01")
STRICT_LIVE_COOLDOWN_FLOOR_SECONDS = 3600

MINIMUM_SCORE_BOUNDS = (50, 95)
MINIMUM_LIQUIDITY_USD_BOUNDS = (Decimal("5000"), Decimal("1000000"))
MINIMUM_24H_VOLUME_USD_BOUNDS = (Decimal("10000"), Decimal("10000000"))
MAXIMUM_RELATIVE_SPREAD_BOUNDS = (Decimal("0.01"), Decimal("0.15"))
MAXIMUM_ABSOLUTE_SPREAD_BOUNDS = (Decimal("0.01"), Decimal("0.08"))
MAXIMUM_SLIPPAGE_BOUNDS = (Decimal("0.005"), Decimal("0.05"))
COOLDOWN_SECONDS_BOUNDS = (900, 86400)
ALLOCATION_PRESETS_USD = (
    Decimal("25"),
    Decimal("50"),
    Decimal("100"),
    Decimal("250"),
    Decimal("500"),
    Decimal("1000"),
)

_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _bounded(
    value: Decimal,
    name: str,
    bounds: tuple[Decimal, Decimal],
) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"{name} must be between {bounds[0]} and {bounds[1]}")


@dataclass(frozen=True)
class CategoryAllowlist:
    all_categories: bool = True
    category_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.category_ids, frozenset):
            raise TypeError("category_ids must be a frozenset")
        if self.all_categories and self.category_ids:
            raise ValueError("all-categories mode cannot include explicit categories")
        if not self.all_categories and not self.category_ids:
            raise ValueError("custom category allowlist must not be empty")
        for category_id in self.category_ids:
            if not isinstance(category_id, str) or not _CATEGORY_RE.fullmatch(category_id):
                raise ValueError("category identifier is malformed")


@dataclass(frozen=True)
class QuietHours:
    start: time
    end: time

    def __post_init__(self) -> None:
        if not isinstance(self.start, time) or not isinstance(self.end, time):
            raise TypeError("quiet hours require time values")
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("quiet hours are local wall times without tzinfo")
        if self.start == self.end:
            raise ValueError("quiet-hour start and end must differ")


@dataclass(frozen=True)
class OpportunityScannerSettings:
    revision: int = 0
    minimum_score: int = BALANCED_MINIMUM_SCORE
    minimum_liquidity_usd: Decimal = BALANCED_MINIMUM_LIQUIDITY_USD
    minimum_24h_volume_usd: Decimal = BALANCED_MINIMUM_24H_VOLUME_USD
    maximum_relative_spread: Decimal = BALANCED_MAXIMUM_RELATIVE_SPREAD
    maximum_absolute_spread: Decimal = BALANCED_MAXIMUM_ABSOLUTE_SPREAD
    maximum_slippage: Decimal = BALANCED_MAXIMUM_SLIPPAGE
    paper_allocation_usd: Decimal = BALANCED_PAPER_ALLOCATION_USD
    categories: CategoryAllowlist = field(default_factory=CategoryAllowlist)
    live_mode: LiveMode = LiveMode.OFF
    live_edge_enabled: bool = False
    cooldown_seconds: int = BALANCED_COOLDOWN_SECONDS
    alert_mode: AlertMode = AlertMode.ON_DEMAND_ONLY
    quiet_hours: QuietHours | None = None
    timezone_name: str = "UTC"

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if (
            not isinstance(self.minimum_score, int)
            or not MINIMUM_SCORE_BOUNDS[0]
            <= self.minimum_score
            <= MINIMUM_SCORE_BOUNDS[1]
        ):
            raise ValueError(
                f"minimum_score must be between {MINIMUM_SCORE_BOUNDS[0]} "
                f"and {MINIMUM_SCORE_BOUNDS[1]}"
            )
        _bounded(
            self.minimum_liquidity_usd,
            "minimum_liquidity_usd",
            MINIMUM_LIQUIDITY_USD_BOUNDS,
        )
        _bounded(
            self.minimum_24h_volume_usd,
            "minimum_24h_volume_usd",
            MINIMUM_24H_VOLUME_USD_BOUNDS,
        )
        _bounded(
            self.maximum_relative_spread,
            "maximum_relative_spread",
            MAXIMUM_RELATIVE_SPREAD_BOUNDS,
        )
        _bounded(
            self.maximum_absolute_spread,
            "maximum_absolute_spread",
            MAXIMUM_ABSOLUTE_SPREAD_BOUNDS,
        )
        _bounded(
            self.maximum_slippage,
            "maximum_slippage",
            MAXIMUM_SLIPPAGE_BOUNDS,
        )
        if self.paper_allocation_usd not in ALLOCATION_PRESETS_USD:
            raise ValueError("paper_allocation_usd must be an approved preset")
        if not isinstance(self.categories, CategoryAllowlist):
            raise TypeError("categories must be CategoryAllowlist")
        if not isinstance(self.live_mode, LiveMode):
            raise ValueError("live_mode must be OFF or STRICT")
        if not isinstance(self.live_edge_enabled, bool):
            raise ValueError("live_edge_enabled must be a bool")
        if (
            not isinstance(self.cooldown_seconds, int)
            or not COOLDOWN_SECONDS_BOUNDS[0]
            <= self.cooldown_seconds
            <= COOLDOWN_SECONDS_BOUNDS[1]
        ):
            raise ValueError(
                f"cooldown_seconds must be between {COOLDOWN_SECONDS_BOUNDS[0]} "
                f"and {COOLDOWN_SECONDS_BOUNDS[1]}"
            )
        if not isinstance(self.alert_mode, AlertMode):
            raise ValueError("alert_mode must be ON_DEMAND_ONLY or INSTANT")
        if self.quiet_hours is not None and not isinstance(self.quiet_hours, QuietHours):
            raise TypeError("quiet_hours must be QuietHours or None")
        if not isinstance(self.timezone_name, str) or not self.timezone_name:
            raise ValueError("timezone_name is required")
        if self.timezone_name != self.timezone_name.strip():
            raise ValueError("timezone_name must not contain surrounding whitespace")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc


def balanced_settings(*, revision: int = 0) -> OpportunityScannerSettings:
    return OpportunityScannerSettings(revision=revision)
