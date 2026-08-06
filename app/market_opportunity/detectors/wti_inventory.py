"""WTI inventory surprise Stage A detector (V4.2)."""

from __future__ import annotations

from decimal import Decimal

from app.market_opportunity.detectors.base import (
    MIN_INVENTORY_POINTS,
    WTI_INVENTORY_DETECTOR_ID,
    DetectorCandidate,
    WtiDetectorContext,
)
from app.market_opportunity.indicators import IndicatorError, inventory_wow_zscore
from app.market_opportunity.models import OpportunityFamily, OpportunitySide
from app.market_opportunity.sources.eia import InventoryPoint

INVENTORY_Z_CANDIDATE = Decimal("1.0")
INVENTORY_Z_STRONG = Decimal("1.5")


class WtiInventoryDetector:
    """Inventory surprise: |z| >= 1.0; draw→LONG, build→SHORT.

    Inventory alone must not publish — pipeline requires same-side structure
    confluence from trend or breakout.
    """

    @property
    def detector_id(self) -> str:
        return WTI_INVENTORY_DETECTOR_ID

    @property
    def family(self) -> OpportunityFamily:
        return OpportunityFamily.COMMODITY_SUPPLY_DEMAND

    def detect(self, context: object) -> list[DetectorCandidate]:
        points = _inventory_from_context(context)
        if len(points) < MIN_INVENTORY_POINTS:
            return []
        try:
            z = inventory_wow_zscore(points, window=26)
        except IndicatorError:
            return []
        abs_z = abs(z)
        if abs_z < INVENTORY_Z_CANDIDATE:
            return []
        # Draw (negative WoW surprise / stocks falling) → bullish oil → LONG
        # Build (positive) → bearish oil → SHORT
        # z is on WoW Δ stocks; positive z = larger build than average → SHORT
        if z <= -INVENTORY_Z_CANDIDATE:
            side = OpportunitySide.LONG
            reason = "inventory_draw_z"
        elif z >= INVENTORY_Z_CANDIDATE:
            side = OpportunitySide.SHORT
            reason = "inventory_build_z"
        else:
            return []
        strong = abs_z >= INVENTORY_Z_STRONG
        extras = {
            "z_score": str(z),
            "abs_z": str(abs_z),
            "strong": strong,
            "period": points[-1].period.isoformat(),
            "inventory_value": str(points[-1].value),
        }
        return [
            DetectorCandidate(
                side=side,
                detector_id=self.detector_id,
                reason=reason,
                extras=extras,
            )
        ]


def _inventory_from_context(context: object) -> list[InventoryPoint]:
    if isinstance(context, WtiDetectorContext):
        return list(context.inventory)
    inventory = getattr(context, "inventory", None)
    if inventory is None and isinstance(context, dict):
        inventory = context.get("inventory")
    if not inventory:
        return []
    return list(inventory)
