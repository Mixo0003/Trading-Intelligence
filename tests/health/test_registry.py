"""V4.2.1 registry completeness and safe labels."""

from __future__ import annotations

from app.database.storage import initialize_database
from app.health.monitor import run_health_check
from app.health.registry import (
    SAFE_GENERIC_LABEL_UK,
    all_registered_ids,
    all_specs,
    label_uk_for,
)
from tests.conftest import make_test_settings

# Components currently emitted by run_health_check / storage alerts.
_EXPECTED_EMITTED = frozenset(
    {
        "telegram",
        "scheduler",
        "sqlite",
        "infrastructure",
        "weather",
        "copy_intel",
        "copy_readonly",
        "crypto_intel",
        "crypto_readonly",
        "market_opportunity",
        "opportunity_discovery",
        "opportunity_books",
        "opportunity_history",
        "opportunity_scheduler",
        "opportunity_storage",
        "opportunity_delivery",
        "opportunity_baseline",
        "storage",
        "health_monitor",
        "overall",
    }
)


def test_registry_covers_expected_emitted_components():
    registered = all_registered_ids()
    missing = _EXPECTED_EMITTED - registered
    assert not missing, f"Unregistered emitted components: {sorted(missing)}"


def test_registry_metadata_complete():
    for spec in all_specs():
        assert spec.label_uk.strip()
        assert spec.product_area_uk.strip()
        assert spec.capability_uk.strip()
        assert spec.impact_degraded_uk.strip()
        assert spec.impact_down_uk.strip()
        assert spec.fail_safe_uk.strip()
        assert spec.admin_action_down_uk.strip()
        assert spec.ui_group.strip()
        assert spec.reason_codes


def test_unregistered_label_never_echoes_id():
    weird = "totally_unknown_component_xyz"
    assert label_uk_for(weird) == SAFE_GENERIC_LABEL_UK
    assert weird not in label_uk_for(weird)


def test_probe_emits_only_registered_components(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "reg.db"),
        health_monitor_enabled=True,
        opportunity_scanner_enabled=True,
        market_opportunity_enabled=True,
    )
    initialize_database(settings)
    overall = run_health_check(settings)
    registered = all_registered_ids()
    for comp in overall.components:
        assert comp.component in registered, comp.component
