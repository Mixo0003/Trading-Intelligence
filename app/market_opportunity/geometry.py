"""Deterministic entry / invalidation / target geometry for V4.2 WTI."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.market_opportunity.indicators import atr14, prev_20_high_low, sma
from app.market_opportunity.models import OpportunitySide
from app.market_opportunity.sources.eia import DailyBar

StructureKind = Literal["trend", "breakout"]


class GeometryError(ValueError):
    """Invalid or inverted opportunity geometry."""


@dataclass(frozen=True)
class OpportunityGeometry:
    side: OpportunitySide
    structure: StructureKind
    price: Decimal
    atr: Decimal
    invalidation: Decimal
    entry_low: Decimal
    entry_high: Decimal
    entry_mid: Decimal
    r: Decimal
    t1: Decimal
    t2: Decimal


def _require_positive(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise GeometryError(f"{name} must be a finite Decimal")
    if value <= 0:
        raise GeometryError(f"{name} must be > 0")
    return value


def build_geometry(
    bars: list[DailyBar] | tuple[DailyBar, ...],
    *,
    side: OpportunitySide,
    structure: StructureKind,
) -> OpportunityGeometry:
    """Build V4.2 geometry for trend/breakout LONG or SHORT.

    Rules (Final Plan):
    - Trend LONG inv: min(SMA20, lowest close last 5) - 0.1·ATR
    - Trend SHORT inv: max(SMA20, highest close last 5) + 0.1·ATR
    - Breakout LONG inv: prev20_high - 0.1·ATR
    - Breakout SHORT inv: prev20_low + 0.1·ATR
    - LONG entry: [P - 0.25·ATR, P + 0.10·ATR]
    - SHORT entry: [P - 0.10·ATR, P + 0.25·ATR] (mirror)
    - T1 = 1.2R, T2 = 1.5R from entry_mid
    - Reject inverted/invalid geometry (GeometryError)
    """
    if side not in {OpportunitySide.LONG, OpportunitySide.SHORT}:
        raise GeometryError("side must be LONG or SHORT")
    if structure not in {"trend", "breakout"}:
        raise GeometryError("structure must be 'trend' or 'breakout'")
    if len(bars) < 21:
        raise GeometryError("geometry requires at least 21 daily bars")

    price = bars[-1].close
    atr = _require_positive(atr14(bars), "atr")
    tenth = Decimal("0.1") * atr
    sma20 = sma(bars, 20)
    last5 = [b.close for b in bars[-5:]]

    if structure == "trend":
        if side is OpportunitySide.LONG:
            invalidation = min(sma20, min(last5)) - tenth
        else:
            invalidation = max(sma20, max(last5)) + tenth
    else:
        prev_high, prev_low = prev_20_high_low(bars)
        if side is OpportunitySide.LONG:
            invalidation = prev_high - tenth
        else:
            invalidation = prev_low + tenth

    if side is OpportunitySide.LONG:
        entry_low = price - Decimal("0.25") * atr
        entry_high = price + Decimal("0.10") * atr
        # Breakout entries must remain above the breakout invalidation level.
        if structure == "breakout":
            entry_low = max(entry_low, invalidation + (Decimal("0.05") * atr))
    else:
        entry_low = price - Decimal("0.10") * atr
        entry_high = price + Decimal("0.25") * atr
        if structure == "breakout":
            entry_high = min(entry_high, invalidation - (Decimal("0.05") * atr))

    if entry_low > entry_high:
        raise GeometryError("entry zone inverted")

    entry_mid = (entry_low + entry_high) / Decimal("2")
    r = abs(entry_mid - invalidation)
    if r <= 0:
        raise GeometryError("R must be > 0")

    if side is OpportunitySide.LONG:
        if not (invalidation < entry_low):
            raise GeometryError("LONG invalidation not below entry zone")
        t1 = entry_mid + Decimal("1.2") * r
        t2 = entry_mid + Decimal("1.5") * r
        if not (invalidation < entry_low <= entry_mid <= entry_high < t1 <= t2):
            raise GeometryError("LONG geometry ordering invalid")
    else:
        if not (invalidation > entry_high):
            raise GeometryError("SHORT invalidation not above entry zone")
        t1 = entry_mid - Decimal("1.2") * r
        t2 = entry_mid - Decimal("1.5") * r
        if not (t2 <= t1 < entry_low <= entry_mid <= entry_high < invalidation):
            raise GeometryError("SHORT geometry ordering invalid")

    # T1/T2 are constructed as 1.2R / 1.5R from entry_mid — no separate ratio gate.

    return OpportunityGeometry(
        side=side,
        structure=structure,
        price=price,
        atr=atr,
        invalidation=invalidation,
        entry_low=entry_low,
        entry_high=entry_high,
        entry_mid=entry_mid,
        r=r,
        t1=t1,
        t2=t2,
    )
