"""Detector protocol for extensible opportunity families."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.market_opportunity.models import OpportunityFamily


@runtime_checkable
class Detector(Protocol):
    """Opportunity detector. V4.1 ships none enabled for production."""

    @property
    def detector_id(self) -> str: ...

    @property
    def family(self) -> OpportunityFamily: ...

    def detect(self, context: object) -> list[object]:
        """Return candidates. Must not be invoked by V4.1 live scheduler paths."""
        ...
