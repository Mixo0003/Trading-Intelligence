"""Ensure copy-intel Phase 2 does not touch Weather V1.1 modules."""

from __future__ import annotations

import pathlib


def test_copy_intel_does_not_import_weather_scan():
    root = pathlib.Path(__file__).resolve().parents[2]
    forbidden_imports = (
        "app.weather",
        "app.signals.scan_service",
        "app.signals.event_analysis",
        "app.signals.temperature_analysis",
    )
    hits = []
    for path in list((root / "app" / "copy_intel").glob("**/*.py")) + list(
        (root / "app" / "telegram" / "copy_intel").glob("**/*.py")
    ):
        text = path.read_text(encoding="utf-8")
        for token in forbidden_imports:
            if token in text:
                hits.append(f"{path.name}:{token}")
    assert hits == []


def test_weather_scan_service_has_no_copy_intel_import():
    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "app" / "signals" / "scan_service.py"
    text = path.read_text(encoding="utf-8")
    assert "copy_intel" not in text
    assert "discovery_scoring" not in text
