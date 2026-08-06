"""V4.2.1 alert policy and Ukrainian diagnostics."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.database.storage import initialize_database
from app.health.alerts import dispatch_health_alerts, sync_incidents_from_health
from app.health import storage as health_storage
from app.health.diagnostics import format_down_alert, format_grouped_summary
from app.health.models import ComponentStatus, OverallHealth
from app.health.registry import UI_GROUP_ORDER
from tests.conftest import make_test_settings


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "policy.db"),
        admin_ids=[7],
        health_alerts_enabled=True,
        health_failure_threshold=3,
        health_recovery_threshold=2,
        report_timezone="UTC",
    )
    initialize_database(s)
    return s


def _record(notes, settings):
    for incident_id, alert_key, message in notes:
        health_storage.record_alert_sent(
            settings,
            incident_id=incident_id,
            alert_key=alert_key,
            payload={"text": message},
        )


def test_degraded_does_not_alert(settings):
    overall = OverallHealth(
        status="DEGRADED",
        components=(
            ComponentStatus(
                "opportunity_books",
                "DEGRADED",
                "reduced",
                "t",
                {
                    "reason_code": "reduced_book_coverage",
                    "book_failure_ratio": 0.4,
                    "books_valid": 3,
                    "books_failed": 2,
                },
            ),
        ),
        checked_at="t",
        summary="soft",
    )
    notes = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
    assert notes == []


def test_disabled_does_not_alert(settings):
    overall = OverallHealth(
        status="DISABLED",
        components=(
            ComponentStatus("opportunity_delivery", "DISABLED", "disabled", "t"),
        ),
        checked_at="t",
        summary="off",
    )
    assert [n for n in sync_incidents_from_health(settings, overall) if n[2]] == []


def test_warming_up_does_not_alert(settings):
    overall = OverallHealth(
        status="WARMING_UP",
        components=(
            ComponentStatus("opportunity_baseline", "WARMING_UP", "warming", "t"),
        ),
        checked_at="t",
        summary="warm",
    )
    assert [n for n in sync_incidents_from_health(settings, overall) if n[2]] == []


def test_hard_critical_down_alerts_once(settings):
    health_storage.upsert_component_state(
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
        summary="violation",
    )
    first = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
    assert len(first) == 1
    msg = first[0][2]
    assert "Що сталося" in msg
    assert "Вплив" in msg
    assert "Безпека" in msg
    assert "Підтверджено" in msg
    assert "Початок" in msg
    assert "Дія" in msg
    assert "copy_readonly" not in msg
    _record(first, settings)
    second = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
    assert second == []


def test_soft_only_overall_degraded_no_alert(settings):
    overall = OverallHealth(
        status="DEGRADED",
        components=(
            ComponentStatus(
                "opportunity_books",
                "DEGRADED",
                "reduced",
                "t",
                {"reason_code": "reduced_book_coverage"},
            ),
            ComponentStatus(
                "weather",
                "DEGRADED",
                "optional provider",
                "t",
                {"reason_code": "providers_low"},
            ),
        ),
        checked_at="t",
        summary="soft overall",
    )
    notes = [n for n in sync_incidents_from_health(settings, overall) if n[2]]
    assert notes == []


def test_summary_has_no_raw_component_ids(settings):
    overall = OverallHealth(
        status="DEGRADED",
        components=(
            ComponentStatus(
                "opportunity_books",
                "DEGRADED",
                "Книги ордерів: зменшене покриття",
                "t",
                {"reason_code": "reduced_book_coverage", "book_failure_ratio": 0.5},
            ),
        ),
        checked_at="t",
        summary="x",
    )
    text = format_grouped_summary(overall, groups_order=UI_GROUP_ORDER)
    assert "opportunity_books" not in text
    assert "Книги ордерів" in text


def test_down_alert_builder_sections(settings):
    comp = ComponentStatus(
        "sqlite",
        "DOWN",
        "integrity failed",
        "t",
        {"reason_code": "integrity_failed"},
    )
    msg = format_down_alert(
        settings,
        comp,
        reason_code="integrity_failed",
        consecutive_failures=3,
        incident_id=9,
        opened_at="2026-08-06T12:00:00+00:00",
    )
    assert "sqlite" not in msg
    for section in ("Що сталося", "Вплив", "Безпека", "Підтверджено", "Початок", "Дія"):
        assert section in msg


@pytest.mark.asyncio
async def test_alerts_only_to_admin(settings):
    health_storage.upsert_component_state(
        settings,
        component="telegram",
        status="DOWN",
        consecutive_failures=2,
        consecutive_successes=0,
        detail="poller conflict",
        metrics={"reason_code": "poller_conflict"},
    )
    overall = OverallHealth(
        status="DOWN",
        components=(
            ComponentStatus(
                "telegram",
                "DOWN",
                "poller conflict",
                "t",
                {"reason_code": "poller_conflict"},
            ),
        ),
        checked_at="t",
        summary="conflict",
    )
    send = AsyncMock()
    sent = await dispatch_health_alerts(settings, overall, send)
    assert sent >= 1
    for call in send.await_args_list:
        assert call.args[0] == 7
        assert "telegram" not in call.args[1]
