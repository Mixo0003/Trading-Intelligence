"""WTI breakout Stage A detector (V4.2)."""

from __future__ import annotations

from decimal import Decimal

from app.market_opportunity.detectors.base import (
    MIN_DAILY_BARS,
    WTI_BREAKOUT_DETECTOR_ID,
    DetectorCandidate,
    WtiDetectorContext,
)
from app.market_opportunity.indicators import IndicatorError, atr14, prev_20_high_low
from app.market_opportunity.models import OpportunityFamily, OpportunitySide
from app.market_opportunity.sources.eia import DailyBar


class WtiBreakoutDetector:
    """Breakout LONG/SHORT vs prior 20-day high/low ± 0.1·ATR."""

    @property
    def detector_id(self) -> str:
        return WTI_BREAKOUT_DETECTOR_ID

    @property
    def family(self) -> OpportunityFamily:
        return OpportunityFamily.BREAKOUT

    def detect(self, context: object) -> list[DetectorCandidate]:
        bars = _bars_from_context(context)
        if len(bars) < MIN_DAILY_BARS:
            return []
        try:
            atr = atr14(bars)
            prev_high, prev_low = prev_20_high_low(bars)
        except IndicatorError:
            return []
        if atr <= 0:
            return []
        close = bars[-1].close
        buffer = Decimal("0.1") * atr
        extras = {
            "close": str(close),
            "atr14": str(atr),
            "prev20_high": str(prev_high),
            "prev20_low": str(prev_low),
            "period": bars[-1].period.isoformat(),
            "structure": "breakout",
        }
        if close > prev_high + buffer:
            return [
                DetectorCandidate(
                    side=OpportunitySide.LONG,
                    detector_id=self.detector_id,
                    reason="close_gt_prev20_high_plus_buffer",
                    extras=extras,
                )
            ]
        if close < prev_low - buffer:
            return [
                DetectorCandidate(
                    side=OpportunitySide.SHORT,
                    detector_id=self.detector_id,
                    reason="close_lt_prev20_low_minus_buffer",
                    extras=extras,
                )
            ]
        return []


def _bars_from_context(context: object) -> list[DailyBar]:
    if isinstance(context, WtiDetectorContext):
        return list(context.bars)
    bars = getattr(context, "bars", None)
    if bars is None and isinstance(context, dict):
        bars = context.get("bars")
    if not bars:
        return []
    return list(bars)
