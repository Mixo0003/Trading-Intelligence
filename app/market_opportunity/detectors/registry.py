"""Extensible detector registry — empty live set in V4.1."""

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


# Process-wide registry. V4.1 production never enables live commodity detectors.
default_registry = DetectorRegistry()


def enabled_detectors() -> tuple[str, ...]:
    return default_registry.list_enabled()
