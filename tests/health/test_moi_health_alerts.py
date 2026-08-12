"""MOI health Telegram notification policy regressions (V4.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.database.storage import initialize_database
from app.health.alerts import sync_incidents_from_health
from app.health.models import ComponentStatus, OverallHealth
from app.health.monitor import probe_market_opportunity
from app.health import storage as health_storage
from app.market_opportunity.health import evaluate_moi_health
from app.market_opportunity.scheduler import _write_runtime
from tests.conftest import make_test_settings


def _now() -> datetime:
    return datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _fresh_ok_diag(clock: datetime | None = None) -> dict:
    """EIA periods relative to ``clock`` so age stays inside freshness ceilings."""
    clock = clock or _now()
    return {
        "wti_price": {
            "period": (clock.date() - timedelta(days=2)).isoformat(),
            "bars": 120,
        },
        "wti_inventory": {
            "period": (clock.date() - timedelta(days=7)).isoformat(),
            "points": 40,
        },
        "fred_crosscheck": {"ok": False, "error": "missing_key"},
        "detector_runtime": {"ok": True, "published": 0, "suppressed": 0},
        "paper_outcome_runtime": {"ok": True, "measured": 0},
    }


@pytest.fixture
def freeze_moi_clock(monkeypatch):
    """Freeze ``evaluate_moi_health`` wall clock used by ``probe_market_opportunity``."""
    frozen = _now()

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr("app.market_opportunity.health.datetime", _FrozenDateTime)
    return frozen


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "moi_health_alerts.db"),
        market_opportunity_enabled=True,
        market_opportunity_auto_alerts_enabled=False,
        market_opportunity_read_only=True,
        health_alerts_enabled=True,
        health_failure_threshold=3,
        health_recovery_threshold=2,
        admin_ids=[1],
    )
    initialize_database(s)
    return s


def _moi_overall(comp: ComponentStatus) -> OverallHealth:
    return OverallHealth(
        status=comp.status if comp.status != "DISABLED" else "HEALTHY",
        components=(comp,),
        checked_at="t",
        summary=comp.detail,
    )


def _record(notes, settings):
    for incident_id, alert_key, _ in notes:
        health_storage.record_alert_sent(
            settings, incident_id=incident_id, alert_key=alert_key
        )


class TestMoiHealthAlertPolicy:
    def test_startup_transient_eia_missing_no_telegram(self, settings):
        snap = evaluate_moi_health(settings, diagnostics={}, now=_now())
        assert snap["metrics"]["warming"] is True
        assert snap["state"] == "DEGRADED"
        assert snap["probe_ok"] is True
        assert snap["metrics"]["telegram_alert_policy"] == "suppress"

        comp = probe_market_opportunity(settings)
        notes = sync_incidents_from_health(settings, _moi_overall(comp))
        assert notes == []

    def test_fred_missing_optional_no_telegram(self, settings, freeze_moi_clock):
        diag = _fresh_ok_diag(freeze_moi_clock)
        snap = evaluate_moi_health(
            settings,
            diagnostics=diag,
            now=freeze_moi_clock,
        )
        assert snap["state"] == "DEGRADED"
        assert "FRED" in snap["detail"]
        assert snap["metrics"]["telegram_alert_policy"] == "suppress"
        assert snap["metrics"]["wti_price"]["state"] == "HEALTHY"

        # Persist runtime so probe reads the soft-degrade snapshot.
        from app.database import market_opportunity_storage as moi_storage
        import json

        moi_storage.upsert_runtime_status(
            settings,
            status="ok",
            detail=json.dumps(diag),
            checked_at=freeze_moi_clock,
            enabled_detectors=[],
        )
        comp = probe_market_opportunity(settings)
        assert comp.status == "DEGRADED"
        notes = sync_incidents_from_health(settings, _moi_overall(comp))
        assert notes == []

    def test_repeated_identical_health_state_one_notification(self, settings):
        # Soft DEGRADED must not Telegram; use hard-critical DOWN instead.
        from app.health import storage as hs

        hs.upsert_component_state(
            settings,
            component="copy_readonly",
            status="DOWN",
            consecutive_failures=1,
            consecutive_successes=0,
            detail="READ-ONLY SAFETY VIOLATION",
            metrics={"reason_code": "readonly_violation"},
        )
        overall = OverallHealth(
            status="DOWN",
            components=(
                ComponentStatus(
                    "copy_readonly",
                    "DOWN",
                    "READ-ONLY SAFETY VIOLATION",
                    "t",
                    {"reason_code": "readonly_violation"},
                ),
            ),
            checked_at="t",
            summary="down",
        )
        first = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
        assert len(first) == 1
        assert "copy_readonly" not in first[0][2]
        _record(first, settings)
        second = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
        assert second == []
        third = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
        assert third == []

    def test_persistent_eia_failure_after_threshold_one_alert(self, settings):
        from app.database import market_opportunity_storage as moi_storage
        import json

        moi_storage.upsert_runtime_status(
            settings,
            status="down",
            detail=json.dumps({"wti_price": {"error": "fetch_failed"}}),
            checked_at=_now(),
            enabled_detectors=[],
        )
        notes_accum = []
        for _ in range(settings.health_failure_threshold):
            comp = probe_market_opportunity(settings)
            notes = sync_incidents_from_health(settings, _moi_overall(comp))
            if notes:
                notes_accum.extend(notes)
                _record(notes, settings)
        assert len(notes_accum) == 1
        assert "EIA RWTC missing" in notes_accum[0][2]

        # Further ticks must not re-alert the same failure.
        for _ in range(3):
            comp = probe_market_opportunity(settings)
            assert sync_incidents_from_health(settings, _moi_overall(comp)) == []

    def test_eia_recovery_one_notification(self, settings, freeze_moi_clock):
        from app.database import market_opportunity_storage as moi_storage
        import json

        moi_storage.upsert_runtime_status(
            settings,
            status="down",
            detail=json.dumps({"wti_price": {"error": "fetch_failed"}}),
            checked_at=freeze_moi_clock,
            enabled_detectors=[],
        )
        alerted = False
        for _ in range(settings.health_failure_threshold):
            comp = probe_market_opportunity(settings)
            notes = sync_incidents_from_health(settings, _moi_overall(comp))
            if notes:
                _record(notes, settings)
                alerted = True
        assert alerted

        recovered = _fresh_ok_diag(freeze_moi_clock)
        moi_storage.upsert_runtime_status(
            settings,
            status="ok",
            detail=json.dumps(recovered),
            checked_at=freeze_moi_clock,
            enabled_detectors=[],
        )
        comp = probe_market_opportunity(settings)
        assert comp.metrics["wti_price"]["state"] == "HEALTHY"
        # Component may remain DEGRADED for optional FRED.
        assert comp.status == "DEGRADED"
        notes = sync_incidents_from_health(settings, _moi_overall(comp))
        assert len(notes) == 1
        assert "EIA RWTC is HEALTHY" in notes[0][2]
        _record(notes, settings)
        assert sync_incidents_from_health(settings, _moi_overall(comp)) == []

    def test_runtime_detail_merge_does_not_resurrect_stale_eia_failure(
        self, settings, freeze_moi_clock
    ):
        diag = _fresh_ok_diag(freeze_moi_clock)
        price_period = diag["wti_price"]["period"]
        inv_period = diag["wti_inventory"]["period"]
        _write_runtime(
            settings,
            status="down",
            detail={"wti_price": {"error": "fetch_failed"}, "job": "moi_wti_price_refresh"},
        )
        _write_runtime(
            settings,
            status="ok",
            detail={
                "job": "moi_wti_price_refresh",
                "wti_price": {"period": price_period, "bars": 120},
                "fred_crosscheck": {"ok": False, "error": "missing_key"},
            },
        )
        _write_runtime(
            settings,
            status="ok",
            detail={
                "job": "moi_wti_inventory_refresh",
                "wti_inventory": {"period": inv_period, "points": 40},
            },
        )
        # Outcomes-only write previously wiped EIA — must keep healthy price.
        _write_runtime(
            settings,
            status="ok",
            detail={
                "job": "moi_wti_outcomes",
                "paper_outcome_runtime": {
                    "ok": True,
                    "measured": 0,
                    "period": price_period,
                },
            },
        )
        snap = evaluate_moi_health(settings, now=freeze_moi_clock)
        assert snap["metrics"]["wti_price"]["state"] == "HEALTHY"
        assert price_period in snap["metrics"]["wti_price"]["detail"]
        assert snap["metrics"]["warming"] is False
        # Must not fall back to DOWN/missing after outcomes merge.
        assert snap["state"] == "DEGRADED"  # FRED still optional-missing
        assert snap["metrics"]["telegram_alert_policy"] == "suppress"
