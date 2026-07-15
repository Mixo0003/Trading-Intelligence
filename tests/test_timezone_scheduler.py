from datetime import datetime, timedelta, timezone

from app.config.timezones import load_timezone
from app.telegram.handlers import schedule_report_jobs
from app.telegram.menu import build_telegram_application
from tests.conftest import make_test_settings


def _scheduled_settings():
    return make_test_settings(
        auto_reports_enabled=True,
        report_timezone="America/Chicago",
        report_times=["08:00", "12:00", "20:00"],
    )


def test_america_chicago_and_utc_load():
    chicago = load_timezone("America/Chicago")
    utc = load_timezone("UTC")
    assert datetime(2026, 7, 15, 12, tzinfo=chicago).utcoffset() == timedelta(hours=-5)
    assert datetime(2026, 1, 15, 12, tzinfo=chicago).utcoffset() == timedelta(hours=-6)
    assert datetime(2026, 7, 15, 12, tzinfo=utc).utcoffset() == timedelta(0)


def test_invalid_timezone_logs_and_falls_back(caplog):
    with caplog.at_level("ERROR"):
        result = load_timezone("Invalid/Timezone")
    assert result is timezone.utc
    assert "falling back to UTC" in caplog.text


def test_jobqueue_available_and_registers_once():
    application = build_telegram_application(_scheduled_settings())
    assert application.job_queue is not None
    assert len(application.job_queue.jobs()) == 3

    schedule_report_jobs(application, _scheduled_settings())
    assert len(application.job_queue.jobs()) == 3


def test_restart_builds_fresh_nonduplicated_jobqueue():
    first = build_telegram_application(_scheduled_settings())
    second = build_telegram_application(_scheduled_settings())
    assert first.job_queue is not second.job_queue
    assert len(first.job_queue.jobs()) == 3
    assert len(second.job_queue.jobs()) == 3


def test_auto_reports_disabled_registers_no_jobs():
    settings = make_test_settings(
        auto_reports_enabled=False,
        report_timezone="America/Chicago",
        report_times=["08:00", "12:00", "20:00"],
    )
    application = build_telegram_application(settings)
    assert application.job_queue is not None
    assert application.job_queue.jobs() == ()
