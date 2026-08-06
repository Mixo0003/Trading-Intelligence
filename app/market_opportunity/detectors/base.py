"""Detector protocol for extensible opportunity families."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from app.market_opportunity.models import OpportunityFamily, OpportunitySide
from app.market_opportunity.sources.eia import DailyBar, InventoryPoint

WTI_TREND_DETECTOR_ID = "wti_trend"
WTI_BREAKOUT_DETECTOR_ID = "wti_breakout"
WTI_INVENTORY_DETECTOR_ID = "wti_inventory"

WTI_STRUCTURE_DETECTOR_IDS = frozenset(
    {WTI_TREND_DETECTOR_ID, WTI_BREAKOUT_DETECTOR_ID}
)

MIN_DAILY_BARS = 60
MIN_INVENTORY_POINTS = 27  # 26 WoW window requires window+1 observations


@dataclass(frozen=True)
class DetectorCandidate:
    """Stage A in-memory candidate — never persisted directly."""

    side: OpportunitySide
    detector_id: str
    reason: str
    extras: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WtiDetectorContext:
    """Shared input for WTI detectors (in-memory only)."""

    bars: Sequence[DailyBar]
    inventory: Sequence[InventoryPoint] = ()


@runtime_checkable
class Detector(Protocol):
    """Opportunity detector protocol."""

    @property
    def detector_id(self) -> str: ...

    @property
    def family(self) -> OpportunityFamily: ...

    def detect(self, context: object) -> list[DetectorCandidate]:
        """Return Stage A candidates. Cold-start returns an empty list."""
        ...
