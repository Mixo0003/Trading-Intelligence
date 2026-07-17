"""Tests for health monitor status, thresholds, alerts, and sanitize."""

from __future__ import annotations

import pytest

from app.health.aggregator import aggregate_overall
from app.health.alerts import sync_incidents_from_health
from app.health.models import ComponentStatus, OverallHealth
from app.health.monitor import run_health_check
from app.health.sanitize import hash_user_id, sanitize_config_summary, sanitize_mapping
from app.health.thresholds import apply_consecutive_thresholds
from app.health import storage as health_storage
from app.database.storage import initialize_database
from tests.conftest import make_test_settings


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(database_path=str(tmp_path / "health.db"))
    initialize_database(s)
    return s


class TestThresholds:
    def test_failure_and_recovery_thresholds(self, settings):
        status, fails, succs = apply_consecutive_thresholds(
            settings, probe_ok=True, consecutive_failures=0, consecutive_successes=0
        )
        assert status == "HEALTHY"

        status, fails, succs = "HEALTHY", 0, 0
        for _ in range(3):
            status, fails, succs = apply_consecutive_thresholds(
                settings,
                probe_ok=False,
                consecutive_failures=fails,
                consecutive_successes=succs,
            )
        assert status == "DEGRADED"
        assert fails == 3

        status, fails, succs = apply_consecutive_thresholds(
            settings,
            probe_ok=True,
            consecutive_failures=fails,
            consecutive_successes=0,
        )
        assert status == "DEGRADED"
        status, fails, succs = apply_consecutive_thresholds(
            settings,
            probe_ok=True,
            consecutive_failures=0,
            consecutive_successes=succs,
        )
        assert status == "HEALTHY"

    def test_hard_down(self, settings):
        status, _, _ = apply_consecutive_thresholds(
            settings,
            probe_ok=False,
            consecutive_failures=0,
            consecutive_successes=5,
            hard_down=True,
        )
        assert status == "DOWN"


class TestAggregation:
    def test_critical_down(self):
        comps = [
            ComponentStatus("sqlite", "DOWN", "fail", "t"),
            ComponentStatus("weather", "HEALTHY", "ok", "t"),
        ]
        assert aggregate_overall(comps).status == "DOWN"

    def test_degraded_noncritical(self):
        comps = [
            ComponentStatus("sqlite", "HEALTHY", "ok", "t"),
            ComponentStatus("weather", "DOWN", "fail", "t"),
        ]
        assert aggregate_overall(comps).status == "DEGRADED"


class TestSanitize:
    def test_secrets_redacted(self, settings):
        cfg = sanitize_config_summary(settings)
        assert cfg["telegram_token"] == "***"
        assert cfg["anthropic_api_key"] == "***"
        assert "token" not in str(sanitize_mapping({"api_key": "secret123"})).lower() or "***" in str(
            sanitize_mapping({"api_key": "secret123"})
        )
        assert sanitize_mapping({"api_key": "secret123"})["api_key"] == "***"

    def test_user_hash_stable(self):
        assert hash_user_id(42) == hash_user_id(42)
        assert hash_user_id(42) != hash_user_id(43)


class TestMonitorAndAlerts:
    def test_run_health_check(self, settings):
        overall = run_health_check(settings, bot_data={"health_polling_active": True})
        assert overall.status in {"HEALTHY", "DEGRADED", "DOWN"}
        names = {c.component for c in overall.components}
        assert "sqlite" in names
        assert "copy_readonly" in names

    def test_alert_deduplication(self, settings):
        overall = OverallHealth(
            status="DEGRADED",
            components=(
                ComponentStatus("weather", "DEGRADED", "providers low", "t"),
            ),
            checked_at="t",
            summary="degraded",
        )
        first = sync_incidents_from_health(settings, overall)
        # Mark alerts sent
        for incident_id, alert_key, _ in first:
            health_storage.record_alert_sent(
                settings, incident_id=incident_id, alert_key=alert_key
            )
        second = sync_incidents_from_health(settings, overall)
        # updated transition for same degraded may appear but alert keys differ;
        # open should not repeat
        open_keys = [k for _, k, _ in second if k.endswith(":open")]
        assert open_keys == []

    def test_readonly_violation_down(self, settings):
        bad = make_test_settings(
            database_path=settings.database_path,
            copy_intel_enabled=True,
            copy_intel_read_only=False,
        )
        overall = run_health_check(bad)
        ro = next(c for c in overall.components if c.component == "copy_readonly")
        assert ro.status == "DOWN"
        assert overall.status == "DOWN"

    def test_provider_and_db_present(self, settings):
        health_storage.upsert_provider_metric(
            settings, "nws", success=True, status_code=200
        )
        overall = run_health_check(settings)
        assert any(c.component == "weather" for c in overall.components)
        assert any(c.component == "sqlite" and c.status == "HEALTHY" for c in overall.components)
