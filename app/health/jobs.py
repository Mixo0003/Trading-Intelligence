"""Job execution instrumentation for the health monitor."""

from __future__ import annotations

from datetime import datetime, timezone

from app.config.settings import Settings
from app.health import storage as health_storage


def record_job_execution(
    settings: Settings,
    *,
    job_name: str,
    status: str,
    started_at: str | None = None,
    expected_interval_seconds: int | None = None,
) -> int:
    return health_storage.insert_job_execution(
        settings,
        job_name=job_name,
        status=status,
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
        expected_interval_seconds=expected_interval_seconds,
    )


def finish_job_execution(
    settings: Settings,
    *,
    job_name: str,
    status: str,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    health_storage.finish_latest_job_execution(
        settings,
        job_name=job_name,
        status=status,
        duration_ms=duration_ms,
        error=error,
    )
