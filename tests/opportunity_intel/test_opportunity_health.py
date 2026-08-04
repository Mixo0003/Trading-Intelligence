"""Checkpoint 7 — opportunity health states and sanitization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.database.opportunity_storage import (
    initialize_opportunity_schema,
    save_runtime_snapshot,
    save_scanner_state,
)
from app.database.storage import initialize_database
from app.health.monitor import probe_opportunity_components, run_health_check
from app.opportunity_intel.health import evaluate_opportunity_health
from app.opportunity_intel.models import OpportunityScannerState, ScannerStatus
from app.opportunity_intel.versions import (
    BASELINE_VERSION,
    OPPORTUNITY_SCHEMA_VERSION,
    OPPORTUNITY_SCORE_VERSION,
)
from tests.conftest import make_test_settings

NOW = datetime(2026, 7, 24, 19, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opp_health.db"),
        opportunity_scanner_enabled=True,
        health_monitor_enabled=True,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    return value


def _scanner(
    *,
    status=ScannerStatus.HEALTHY,
    cycles=2,
    last_complete=None,
):
    return OpportunityScannerState(
        status=status,
        schema_version=OPPORTUNITY_SCHEMA_VERSION,
        baseline_version=BASELINE_VERSION,
        score_version=OPPORTUNITY_SCORE_VERSION,
        global_baseline_cycles=cycles,
        discovery_complete=True,
        last_complete_discovery_at=last_complete or NOW,
        last_complete_cycle_at=last_complete or NOW,
        updated_at=NOW,
    )


def test_disabled_not_down(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "disabled.db"),
        opportunity_scanner_enabled=False,
    )
    initialize_database(settings)
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "DISABLED"
    components = probe_opportunity_components(settings)
    assert all(c.status == "DISABLED" for c in components)


def test_not_initialized(settings):
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "NOT_INITIALIZED"


def test_baselining(settings):
    save_scanner_state(
        settings,
        _scanner(status=ScannerStatus.BASELINING, cycles=1, last_complete=NOW),
    )
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "BASELINING"


def test_healthy(settings):
    save_scanner_state(settings, _scanner())
    save_runtime_snapshot(
        settings,
        {
            "markets_discovered": 10,
            "outcomes_shortlisted": 5,
            "books_valid": 5,
            "books_failed": 0,
            "histories_valid": 5,
            "histories_failed": 0,
            "last_complete_cycle_at": NOW.isoformat(),
            "last_attempt_at": NOW.isoformat(),
            "storage_ok": True,
            "discovery_status": "COMPLETE",
            "skipped_overlaps": 0,
            "failure_categories": {},
        },
    )
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "HEALTHY"


def test_degraded_by_failure_ratio(settings):
    save_scanner_state(settings, _scanner())
    save_runtime_snapshot(
        settings,
        {
            "markets_discovered": 10,
            "books_valid": 1,
            "books_failed": 4,
            "histories_valid": 5,
            "histories_failed": 0,
            "last_complete_cycle_at": NOW.isoformat(),
            "last_attempt_at": NOW.isoformat(),
            "storage_ok": True,
            "discovery_status": "COMPLETE",
            "skipped_overlaps": 0,
            "failure_categories": {},
        },
    )
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "DEGRADED"


def test_stale_by_cycle_age(settings):
    old = NOW - timedelta(seconds=settings.opportunity_scan_interval_seconds * 4)
    save_scanner_state(settings, _scanner(last_complete=old))
    save_runtime_snapshot(
        settings,
        {
            "markets_discovered": 10,
            "books_valid": 5,
            "books_failed": 0,
            "histories_valid": 5,
            "histories_failed": 0,
            "last_complete_cycle_at": old.isoformat(),
            "last_attempt_at": NOW.isoformat(),
            "storage_ok": True,
            "discovery_status": "COMPLETE",
        },
    )
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "STALE"


def test_down_on_storage_failure(settings):
    save_scanner_state(settings, _scanner(status=ScannerStatus.DOWN, cycles=0))
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "DOWN"


def test_delivery_only_degraded(settings, monkeypatch):
    save_scanner_state(settings, _scanner())
    save_runtime_snapshot(
        settings,
        {
            "markets_discovered": 10,
            "books_valid": 5,
            "books_failed": 0,
            "histories_valid": 5,
            "histories_failed": 0,
            "last_complete_cycle_at": NOW.isoformat(),
            "last_attempt_at": NOW.isoformat(),
            "storage_ok": True,
            "discovery_status": "COMPLETE",
            "failure_categories": {},
        },
    )
    monkeypatch.setattr(
        "app.database.opportunity_storage.count_deliveries_by_status",
        lambda _settings: {"pending": 0, "retry_wait": 8, "dead_letter": 2},
    )
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "DEGRADED"
    assert snap.metrics["delivery_dead_letter"] == 2


def test_one_malformed_market_isolation(settings):
    save_scanner_state(settings, _scanner())
    save_runtime_snapshot(
        settings,
        {
            "markets_discovered": 20,
            "outcomes_shortlisted": 10,
            "books_valid": 9,
            "books_failed": 1,
            "histories_valid": 9,
            "histories_failed": 1,
            "last_complete_cycle_at": NOW.isoformat(),
            "last_attempt_at": NOW.isoformat(),
            "storage_ok": True,
            "discovery_status": "COMPLETE",
            "failure_categories": {"history": 1, "book": 1},
        },
    )
    snap = evaluate_opportunity_health(settings, now=NOW)
    assert snap.state == "HEALTHY"


def test_sanitized_diagnostics(settings):
    save_scanner_state(settings, _scanner())
    save_runtime_snapshot(
        settings,
        {
            "markets_discovered": 1,
            "last_complete_cycle_at": NOW.isoformat(),
            "failure_categories": {"book": 1},
            "secret_token": "should-not-matter",
            "path": "C:/Users/secret/db.sqlite",
        },
    )
    overall = run_health_check(settings, bot_data={})
    blob = str(overall)
    assert "secret_token" not in blob
    assert "C:/Users" not in blob
    assert "TELEGRAM_TOKEN" not in blob
    for component in overall.components:
        if component.component.startswith("opportunity_"):
            assert "opportunity_state" in component.metrics
            assert "path" not in component.metrics
            assert "secret_token" not in component.metrics
