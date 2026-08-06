"""Extensible detector registry for Market Opportunity Intelligence."""

from __future__ import annotations

from app.market_opportunity.detectors.base import Detector


class DetectorRegistry:
    def __init__(self) -> None:
        self._detectors: dict[str, Detector] = {}
        self._enabled: set[str] = set()

    def register(self, detector: Detector, *, enabled: bool = False) -> None:
        detector_id = detector.detector_id
        if not detector_id:
            raise ValueError("detector_id required")
        # Hard gate: never register Gold (or other deferred commodities) in V4.2.
        lowered = detector_id.lower()
        if "gold" in lowered:
            raise ValueError("Gold detectors must not be registered in V4.2")
        self._detectors[detector_id] = detector
        if enabled:
            self._enabled.add(detector_id)
        else:
            self._enabled.discard(detector_id)

    def enable(self, detector_id: str) -> None:
        if detector_id not in self._detectors:
            raise KeyError(detector_id)
        self._enabled.add(detector_id)

    def disable(self, detector_id: str) -> None:
        self._enabled.discard(detector_id)

    def get(self, detector_id: str) -> Detector | None:
        return self._detectors.get(detector_id)

    def list_registered(self) -> tuple[str, ...]:
        return tuple(sorted(self._detectors))

    def list_enabled(self) -> tuple[str, ...]:
        return tuple(sorted(self._enabled))

    def clear(self) -> None:
        self._detectors.clear()
        self._enabled.clear()


# Process-wide registry. WTI detectors register via register_wti_detectors.
default_registry = DetectorRegistry()


def enabled_detectors() -> tuple[str, ...]:
    return default_registry.list_enabled()


def register_wti_detectors(
    registry: DetectorRegistry | None = None,
    *,
    enabled: bool = False,
) -> tuple[str, ...]:
    """Register the three V4.2 WTI detectors. Never registers Gold.

    Default ``enabled=False``. Pipeline may also instantiate detector classes
    directly without relying on the enabled set.
    """
    from app.market_opportunity.detectors.wti_breakout import WtiBreakoutDetector
    from app.market_opportunity.detectors.wti_inventory import WtiInventoryDetector
    from app.market_opportunity.detectors.wti_trend import WtiTrendDetector

    target = registry if registry is not None else default_registry
    detectors = (
        WtiTrendDetector(),
        WtiBreakoutDetector(),
        WtiInventoryDetector(),
    )
    for detector in detectors:
        target.register(detector, enabled=enabled)
    return tuple(d.detector_id for d in detectors)
