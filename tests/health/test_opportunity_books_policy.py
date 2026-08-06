"""V4.2.1 opportunity_books isolation and alert policy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.database.opportunity_storage import (
    initialize_opportunity_schema,
    save_runtime_snapshot,
    save_scanner_state,
)
from app.database.storage import initialize_database
from app.health.alerts import sync_incidents_from_health
from app.health import storage as health_storage
from app.health.models import OverallHealth
from app.health.monitor import probe_opportunity_components
from app.opportunity_intel.health import evaluate_component_states
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
        database_path=str(tmp_path / "books_policy.db"),
        opportunity_scanner_enabled=True,
        health_monitor_enabled=True,
        health_alerts_enabled=True,
        health_failure_threshold=3,
        admin_ids=[1],
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    return value


def _scanner():
    return OpportunityScannerState(
        status=ScannerStatus.HEALTHY,
        schema_version=OPPORTUNITY_SCHEMA_VERSION,
        baseline_version=BASELINE_VERSION,
        score_version=OPPORTUNITY_SCORE_VERSION,
        global_baseline_cycles=2,
        discovery_complete=True,
        last_complete_discovery_at=NOW,
        last_complete_cycle_at=NOW,
        updated_at=NOW,
    )


def _runtime(**overrides):
    base = {
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
        "delivery_pending": 0,
    }
    base.update(overrides)
    return base


def test_reduced_coverage_degraded_no_telegram(settings):
    save_scanner_state(settings, _scanner())
    save_runtime_snapshot(
        settings,
        _runtime(books_valid=1, books_failed=4),
    )
    snaps = evaluate_component_states(settings, now=NOW)
    assert snaps["opportunity_books"].state == "DEGRADED"
    comps = probe_opportunity_components(settings)
    books = next(c for c in comps if c.component == "opportunity_books")
    assert books.status == "DEGRADED"
    overall = OverallHealth(
        status="DEGRADED",
        components=(books,),
        checked_at="t",
        summary="books",
    )
    notes = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
    assert notes == []
    for note in sync_incidents_from_health(settings, overall):
        assert "opportunity_books" not in note[2]


def test_books_do_not_inherit_delivery_degraded(settings):
    save_scanner_state(settings, _scanner())
    save_runtime_snapshot(
        settings,
        _runtime(
            books_valid=5,
            books_failed=0,
            failure_categories={},
        ),
    )
    snaps = evaluate_component_states(settings, now=NOW)
    assert snaps["opportunity_books"].state == "HEALTHY"


def test_zero_valid_books_down_alerts_after_threshold(settings):
    save_scanner_state(settings, _scanner())
    save_runtime_snapshot(
        settings,
        _runtime(books_valid=0, books_failed=5),
    )
    notes_accum = []
    for _ in range(settings.health_failure_threshold):
        comps = probe_opportunity_components(settings)
        books = next(c for c in comps if c.component == "opportunity_books")
        assert books.status == "DOWN"
        overall = OverallHealth(
            status="DEGRADED",
            components=(books,),
            checked_at="t",
            summary="books down",
        )
        notes = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
        if notes:
            notes_accum.extend(notes)
            for incident_id, alert_key, message in notes:
                assert "opportunity_books" not in message
                health_storage.record_alert_sent(
                    settings, incident_id=incident_id, alert_key=alert_key
                )
    assert len(notes_accum) == 1
    assert "Що сталося" in notes_accum[0][2]

    # No duplicate
    comps = probe_opportunity_components(settings)
    books = next(c for c in comps if c.component == "opportunity_books")
    overall = OverallHealth(
        status="DEGRADED", components=(books,), checked_at="t", summary="x"
    )
    assert [n for n in sync_incidents_from_health(settings, overall) if n[2]] == []
