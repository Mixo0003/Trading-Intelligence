"""Detector package — V4.2 WTI detectors; never register Gold."""

from __future__ import annotations

from app.market_opportunity.detectors.base import (
    WTI_BREAKOUT_DETECTOR_ID,
    WTI_INVENTORY_DETECTOR_ID,
    WTI_STRUCTURE_DETECTOR_IDS,
    WTI_TREND_DETECTOR_ID,
    Detector,
    DetectorCandidate,
    WtiDetectorContext,
)
from app.market_opportunity.detectors.registry import (
    DetectorRegistry,
    default_registry,
    enabled_detectors,
    register_wti_detectors,
)
from app.market_opportunity.detectors.wti_breakout import WtiBreakoutDetector
from app.market_opportunity.detectors.wti_inventory import WtiInventoryDetector
from app.market_opportunity.detectors.wti_trend import WtiTrendDetector

__all__ = [
    "Detector",
    "DetectorCandidate",
    "DetectorRegistry",
    "WTI_BREAKOUT_DETECTOR_ID",
    "WTI_INVENTORY_DETECTOR_ID",
    "WTI_STRUCTURE_DETECTOR_IDS",
    "WTI_TREND_DETECTOR_ID",
    "WtiBreakoutDetector",
    "WtiDetectorContext",
    "WtiInventoryDetector",
    "WtiTrendDetector",
    "default_registry",
    "enabled_detectors",
    "register_wti_detectors",
]
