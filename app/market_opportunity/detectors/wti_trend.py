"""WTI trend/momentum Stage A detector (V4.2)."""

from __future__ import annotations

from app.market_opportunity.detectors.base import (
    MIN_DAILY_BARS,
    WTI_TREND_DETECTOR_ID,
    DetectorCandidate,
    WtiDetectorContext,
)
from app.market_opportunity.indicators import IndicatorError, sma
from app.market_opportunity.models import OpportunityFamily, OpportunitySide
from app.market_opportunity.sources.eia import DailyBar


class WtiTrendDetector:
    """Trend LONG: close > SMA20 > SMA50; SHORT: close < SMA20 < SMA50."""

    @property
    def detector_id(self) -> str:
        return WTI_TREND_DETECTOR_ID

    @property
    def family(self) -> OpportunityFamily:
        return OpportunityFamily.TREND_MOMENTUM

    def detect(self, context: object) -> list[DetectorCandidate]:
        bars = _bars_from_context(context)
        if len(bars) < MIN_DAILY_BARS:
            return []
        try:
            sma20 = sma(bars, 20)
            sma50 = sma(bars, 50)
        except IndicatorError:
            return []
        close = bars[-1].close
        extras = {
            "close": str(close),
            "sma20": str(sma20),
            "sma50": str(sma50),
            "period": bars[-1].period.isoformat(),
            "structure": "trend",
        }
        if close > sma20 > sma50:
            return [
                DetectorCandidate(
                    side=OpportunitySide.LONG,
                    detector_id=self.detector_id,
                    reason="close_gt_sma20_gt_sma50",
                    extras=extras,
                )
            ]
        if close < sma20 < sma50:
            return [
                DetectorCandidate(
                    side=OpportunitySide.SHORT,
                    detector_id=self.detector_id,
                    reason="close_lt_sma20_lt_sma50",
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
