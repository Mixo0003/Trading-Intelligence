"""Detector package — no live Gold/WTI/Silver/NatGas detectors in V4.1."""

from __future__ import annotations

from app.market_opportunity.detectors.registry import (
    DetectorRegistry,
    default_registry,
    enabled_detectors,
)

__all__ = ["DetectorRegistry", "default_registry", "enabled_detectors"]
