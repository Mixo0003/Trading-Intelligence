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
    # DST: mid-summer CDT (UTC-5), mid-winter CST (UTC-6).
    assert datetime(2026, 7, 15, 12, tzinfo=chicago).utcoffset() == timedelta(hours=-5)
    assert datetime(2026, 1, 15, 12, tzinfo=chicago).utcoffset() == timedelta(hours=-6)
    assert datetime(2026, 7, 15, 12, tzinfo=utc).utcoffset() == timedelta(0)
    # Aware conversion near local midnight must not raise naive/aware errors.
    local = datetime(2026, 7, 15, 0, 30, tzinfo=chicago)
    as_utc = local.astimezone(timezone.utc)
    assert as_utc.tzinfo is not None
    assert as_utc == datetime(2026, 7, 15, 5, 30, tzinfo=timezone.utc)


def test_invalid_timezone_logs_and_falls_back(caplog):
    with caplog.at_level("ERROR"):
        result = load_timezone("Invalid/Timezone")
    assert result is timezone.utc
    assert "falling back to UTC" in caplog.text


def _job_names(application) -> list[str]:
    return [job.name for job in application.job_queue.jobs()]


def test_jobqueue_available_and_registers_once():
    application = build_telegram_application(_scheduled_settings())
    assert application.job_queue is not None
    names = _job_names(application)
    assert sum(1 for name in names if name and name.startswith("report_")) == 3
    assert names.count("health_monitor_tick") == 1
    assert len(names) == 4

    schedule_report_jobs(application, _scheduled_settings())
    names_again = _job_names(application)
    assert sum(1 for name in names_again if name and name.startswith("report_")) == 3
    assert names_again.count("health_monitor_tick") == 1
    assert len(names_again) == 4


def test_restart_builds_fresh_nonduplicated_jobqueue():
    first = build_telegram_application(_scheduled_settings())
    second = build_telegram_application(_scheduled_settings())
    assert first.job_queue is not second.job_queue
    assert len(_job_names(first)) == 4
    assert len(_job_names(second)) == 4
    assert _job_names(first).count("health_monitor_tick") == 1
    assert _job_names(second).count("health_monitor_tick") == 1


def test_auto_reports_disabled_registers_no_jobs():
    settings = make_test_settings(
        auto_reports_enabled=False,
        report_timezone="America/Chicago",
        report_times=["08:00", "12:00", "20:00"],
    )
    application = build_telegram_application(settings)
    assert application.job_queue is not None
    # Report jobs stay off; V2.1 health monitor still registers its tick job.
    names = _job_names(application)
    assert not any(name and name.startswith("report_") for name in names)
    assert names == ["health_monitor_tick"]
