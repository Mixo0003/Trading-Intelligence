"""Failure / recovery / stale thresholds for health status transitions."""

from __future__ import annotations

from app.config.settings import Settings
from app.health.models import HealthStatus


def apply_consecutive_thresholds(
    settings: Settings,
    *,
    probe_ok: bool,
    consecutive_failures: int,
    consecutive_successes: int,
    hard_down: bool = False,
) -> tuple[HealthStatus, int, int]:
    """
    Update consecutive counters and derive status.

    - DEGRADED after HEALTH_FAILURE_THRESHOLD consecutive failures
    - DOWN on hard_down or failures >= 2x failure threshold
    - HEALTHY after HEALTH_RECOVERY_THRESHOLD consecutive successes following degradation
    - First successful probes (no prior failures) are HEALTHY immediately
    """
    fail_th = settings.health_failure_threshold
    rec_th = settings.health_recovery_threshold

    if hard_down:
        return "DOWN", consecutive_failures + 1, 0

    if probe_ok:
        successes = consecutive_successes + 1
        failures = 0
        # Recovery hysteresis applies only after a streak that actually reached
        # DEGRADED (failures >= fail_th). Sub-threshold blips must not produce
        # HEALTHY -> DEGRADED flap on the next success.
        if consecutive_failures >= fail_th and successes < rec_th:
            return "DEGRADED", failures, successes
        return "HEALTHY", failures, successes

    failures = consecutive_failures + 1
    successes = 0
    if failures >= fail_th * 2:
        return "DOWN", failures, successes
    if failures >= fail_th:
        return "DEGRADED", failures, successes
    return "HEALTHY", failures, successes


def job_stale_seconds(settings: Settings, interval_seconds: int) -> float:
    return interval_seconds * settings.health_job_stale_multiplier


def copy_poll_stale_seconds(settings: Settings) -> float:
    return settings.copy_poll_interval_seconds * 2
