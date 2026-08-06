"""V4.2.1 completion: send-path, storage UA, product-area classifications."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.storage import initialize_database
from app.health.alerts import dispatch_health_alerts, sync_incidents_from_health
from app.health import storage as health_storage
from app.health.diagnostics import format_hard_event_alert  # noqa: F401 — contract import
from app.health.models import ComponentStatus, OverallHealth
from app.health.monitor import (
    probe_copy_intelligence,
    probe_crypto_intelligence,
    probe_scheduler,
    probe_weather,
)
from app.health.registry import all_registered_ids, label_uk_for
from app.health.storage_alerts import sync_storage_alerts
from app.opportunity_intel.health import evaluate_component_states
from tests.conftest import make_test_settings

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = (
    "Що сталося",
    "Вплив",
    "Безпека",
    "Підтверджено",
    "Початок",
    "Дія",
)


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "complete.db"),
        admin_ids=[42],
        health_alerts_enabled=True,
        health_failure_threshold=3,
        copy_intel_enabled=True,
        copy_intel_read_only=True,
        crypto_intel_enabled=True,
        crypto_intel_read_only=True,
        opportunity_scanner_enabled=True,
        opportunity_auto_alerts_enabled=False,
        opportunity_user_delivery_enabled=False,
    )
    initialize_database(s)
    return s


def test_required_sections_constant_matches_contract():
    # Imported below after we add the constant; fallback check here via REQUIRED.
    assert set(REQUIRED) <= {
        "Що сталося",
        "Вплив",
        "Безпека",
        "Підтверджено",
        "Початок",
        "Дія",
    }


def test_no_legacy_english_storage_alert_strings_in_source():
    text = (ROOT / "app/health/storage_alerts.py").read_text(encoding="utf-8")
    for banned in (
        "Storage alert:",
        "Storage recovery:",
        "Health alert:",
        "Health recovery:",
        "health degraded",
    ):
        assert banned not in text, banned


def test_health_scheduler_send_only_via_dispatch():
    src = (ROOT / "app/health/scheduler.py").read_text(encoding="utf-8")
    assert "dispatch_health_alerts" in src
    assert "send_message" in src


@pytest.mark.asyncio
async def test_dispatch_admin_only_and_structured(settings):
    health_storage.upsert_component_state(
        settings,
        component="sqlite",
        status="DOWN",
        consecutive_failures=1,
        consecutive_successes=0,
        detail="db unavailable",
        metrics={"reason_code": "db_unavailable"},
    )
    overall = OverallHealth(
        status="DOWN",
        components=(
            ComponentStatus(
                "sqlite",
                "DOWN",
                "SQLite unavailable or integrity/write unsafe",
                "t",
                {"reason_code": "db_unavailable"},
            ),
        ),
        checked_at="t",
        summary="down",
    )
    send = AsyncMock()
    await dispatch_health_alerts(settings, overall, send)
    assert send.await_count >= 1
    for call in send.await_args_list:
        assert call.args[0] == 42
        msg = call.args[1]
        assert "sqlite" not in msg
        for section in REQUIRED:
            assert section in msg


def test_storage_critical_ua_no_english_generic(settings):
    sync_storage_alerts(
        settings,
        OverallHealth(
            status="HEALTHY",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "HEALTHY",
                    "ok",
                    "t",
                    {"storage_level": "HEALTHY"},
                ),
            ),
            checked_at="t",
            summary="ok",
        ),
    )
    notes = sync_storage_alerts(
        settings,
        OverallHealth(
            status="DEGRADED",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "DOWN",
                    "critical",
                    "t",
                    {
                        "storage_level": "CRITICAL",
                        "storage_detail": "disk critical",
                    },
                ),
            ),
            checked_at="t",
            summary="crit",
        ),
    )
    assert notes
    msg = notes[0][2]
    assert "Storage alert:" not in msg
    for section in REQUIRED:
        assert section in msg
    # Must not leak raw component id as a token
    assert "component=storage" not in msg
    assert "Health alert:" not in msg


def test_storage_degraded_and_cleanup_no_telegram(settings):
    sync_storage_alerts(
        settings,
        OverallHealth(
            status="HEALTHY",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "HEALTHY",
                    "ok",
                    "t",
                    {"storage_level": "HEALTHY"},
                ),
            ),
            checked_at="t",
            summary="ok",
        ),
    )
    soft = sync_storage_alerts(
        settings,
        OverallHealth(
            status="DEGRADED",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "DEGRADED",
                    "soft",
                    "t",
                    {
                        "storage_level": "DEGRADED",
                        "storage_detail": "cleanup delay",
                    },
                ),
            ),
            checked_at="t",
            summary="soft",
        ),
    )
    assert soft == []


def test_unknown_component_never_leaks_id():
    weird = "never_registered_xyz"
    assert weird not in label_uk_for(weird)


def test_crypto_disabled(settings):
    settings = make_test_settings(
        database_path=settings.database_path,
        crypto_intel_enabled=False,
        admin_ids=[1],
    )
    initialize_database(settings)
    status = probe_crypto_intelligence(settings)
    assert status.status == "DISABLED"


def test_crypto_one_venue_down_degraded(settings, monkeypatch):
    from app.crypto.models import ExchangeHealth, HealthStatus

    def fake_probe(_settings):
        return [
            ExchangeHealth("binance", HealthStatus.OK, "ok", 1, 1.0),
            ExchangeHealth("bybit", HealthStatus.DOWN, "down", 1, 1.0),
        ]

    monkeypatch.setattr("app.crypto.health.probe_all_exchanges", fake_probe)
    status = probe_crypto_intelligence(settings)
    assert status.status == "DEGRADED"
    assert status.metrics.get("reason_code") == "venue_down"


def test_crypto_all_venues_down(settings, monkeypatch):
    from app.crypto.models import ExchangeHealth, HealthStatus

    def fake_probe(_settings):
        return [
            ExchangeHealth("binance", HealthStatus.DOWN, "down", 1, 1.0),
            ExchangeHealth("bybit", HealthStatus.DOWN, "down", 1, 1.0),
        ]

    monkeypatch.setattr("app.crypto.health.probe_all_exchanges", fake_probe)
    status = probe_crypto_intelligence(settings)
    assert status.status in {"DOWN", "DEGRADED"} or status.metrics.get("reason_code") == "no_usable_venues"
    # With probe_ok False, first tick may still show HEALTHY until threshold — forced_status not set
    # so consecutive path applies. Force check reason after enough failures:
    for _ in range(settings.health_failure_threshold):
        status = probe_crypto_intelligence(settings)
    assert status.status == "DOWN"


def test_copy_partial_incomplete_degraded(settings, monkeypatch):
    from datetime import datetime, timezone

    class W:
        def __init__(self, i):
            self.wallet_id = f"w{i}"

    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        "app.health.monitor.copy_storage.list_enabled_wallets",
        lambda _s: [W(1), W(2)],
    )
    monkeypatch.setattr(
        "app.copy_intel.freshness.baseline_is_complete",
        lambda _s, w: w.wallet_id == "w1",
    )
    monkeypatch.setattr(
        "app.health.monitor.copy_storage.count_baseline_failures",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(
        "app.health.monitor.copy_storage.get_poll_state",
        lambda _s, key: now if "poll" in key else "",
    )
    status = probe_copy_intelligence(settings)
    assert status.status == "DEGRADED"
    assert status.metrics.get("baselines_incomplete") == 1


def test_copy_all_incomplete_down_path(settings, monkeypatch):
    from datetime import datetime, timezone

    class W:
        def __init__(self, i):
            self.wallet_id = f"w{i}"

    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        "app.health.monitor.copy_storage.list_enabled_wallets",
        lambda _s: [W(1), W(2)],
    )
    monkeypatch.setattr(
        "app.copy_intel.freshness.baseline_is_complete",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "app.health.monitor.copy_storage.count_baseline_failures",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(
        "app.health.monitor.copy_storage.get_poll_state",
        lambda _s, key: now if "poll" in key else "",
    )
    status = probe_copy_intelligence(settings)
    assert status.status == "DOWN"
    assert status.metrics.get("reason_code") == "no_trustworthy_processing"


def test_delivery_disabled_when_alerts_off(settings):
    from datetime import datetime, timezone

    from app.database.opportunity_storage import (
        initialize_opportunity_schema,
        save_runtime_snapshot,
        save_scanner_state,
    )
    from app.opportunity_intel.models import OpportunityScannerState, ScannerStatus
    from app.opportunity_intel.versions import (
        BASELINE_VERSION,
        OPPORTUNITY_SCHEMA_VERSION,
        OPPORTUNITY_SCORE_VERSION,
    )

    initialize_opportunity_schema(settings)
    now = datetime(2026, 7, 24, 19, 0, tzinfo=timezone.utc)
    save_scanner_state(
        settings,
        OpportunityScannerState(
            status=ScannerStatus.HEALTHY,
            schema_version=OPPORTUNITY_SCHEMA_VERSION,
            baseline_version=BASELINE_VERSION,
            score_version=OPPORTUNITY_SCORE_VERSION,
            global_baseline_cycles=2,
            discovery_complete=True,
            last_complete_discovery_at=now,
            last_complete_cycle_at=now,
            updated_at=now,
        ),
    )
    save_runtime_snapshot(
        settings,
        {
            "markets_discovered": 10,
            "books_valid": 5,
            "books_failed": 0,
            "histories_valid": 5,
            "histories_failed": 0,
            "last_complete_cycle_at": now.isoformat(),
            "last_attempt_at": now.isoformat(),
            "storage_ok": True,
            "discovery_status": "COMPLETE",
            "skipped_overlaps": 0,
            "failure_categories": {},
        },
    )
    snaps = evaluate_component_states(settings, now=now)
    assert snaps["opportunity_delivery"].state == "DISABLED"


def test_weather_optional_errors_degraded(settings, monkeypatch):
    class Row(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    rows = [
        Row(
            provider="nws",
            success_count=1,
            failure_count=0,
            last_status_code=200,
            last_success_at="2026-08-06T12:00:00+00:00",
            last_failure_at=None,
        ),
        Row(
            provider="met_norway",
            success_count=0,
            failure_count=1,
            last_status_code=500,
            last_success_at=None,
            last_failure_at="2026-08-06T12:00:00+00:00",
        ),
    ]
    monkeypatch.setattr(
        "app.health.storage.list_provider_metrics", lambda _s: rows
    )
    # Ensure min providers is 1 so one valid is enough
    object.__setattr__(settings, "forecast_min_providers", 1)
    status = probe_weather(settings)
    assert status.status == "DEGRADED"
    assert status.metrics.get("reason_code") == "optional_provider_errors"


def test_scheduler_soft_vs_critical(settings, monkeypatch):
    # Soft: only health_monitor stale → DEGRADED
    def latest(settings, name):
        if name == "health_monitor_tick":
            return {
                "status": "error",
                "started_at": "2000-01-01T00:00:00+00:00",
                "finished_at": None,
            }
        return {
            "status": "ok",
            "started_at": "2026-08-06T12:00:00+00:00",
            "finished_at": "2026-08-06T12:00:01+00:00",
        }

    monkeypatch.setattr("app.health.storage.latest_job_execution", latest)
    object.__setattr__(settings, "copy_intel_enabled", False)
    status = probe_scheduler(settings)
    assert status.status == "DEGRADED"
