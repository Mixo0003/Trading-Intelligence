"""Durable storage transition alerts with restart-safe watermarks (Phase 5)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.models import OverallHealth
from app.health.sanitize import sanitize_mapping
from app.storage.backup import backup_status, revalidate_verified_backup_for_pruning
from app.storage.cleanup import get_last_cleanup_report
from app.storage.retention_prune import get_last_prune_report

logger = logging.getLogger(__name__)

_WATERMARK_COMPONENT = "storage_transitions"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _load_watermark(settings: Settings) -> dict[str, Any]:
    row = health_storage.get_component_state(settings, _WATERMARK_COMPONENT)
    if row is None:
        return {}
    raw = row["metrics_json"] if "metrics_json" in row.keys() else None
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_watermark(settings: Settings, watermark: dict[str, Any]) -> None:
    health_storage.upsert_component_state(
        settings,
        component=_WATERMARK_COMPONENT,
        status="HEALTHY",
        consecutive_failures=0,
        consecutive_successes=0,
        detail="storage alert watermark",
        metrics=watermark,
    )


def _enqueue(
    settings: Settings,
    *,
    incident_type: str,
    fingerprint: str,
    severity: str,
    title: str,
    detail: str,
    alert_key: str,
    message: str,
    notifications: list[tuple[int, str, str]],
) -> None:
    if health_storage.was_alert_sent(settings, alert_key):
        return
    incident_id, transition = health_storage.open_or_update_incident(
        settings,
        component="storage",
        incident_type=incident_type,
        fingerprint=fingerprint,
        severity=severity,
        title=title,
        detail=detail,
    )
    if transition not in {"open", "updated"}:
        # Still allow recovery-style keys tied to resolved incidents below.
        pass
    notifications.append((incident_id, alert_key, message))


def _resolve_and_notify(
    settings: Settings,
    *,
    incident_type: str,
    fingerprint: str,
    alert_key: str,
    message: str,
    notifications: list[tuple[int, str, str]],
) -> None:
    if health_storage.was_alert_sent(settings, alert_key):
        return
    incident_id = health_storage.resolve_incident(
        settings,
        component="storage",
        incident_type=incident_type,
        fingerprint=fingerprint,
    )
    if incident_id is None:
        # Create a resolved watermark incident so alert_key can be recorded.
        incident_id, _ = health_storage.open_or_update_incident(
            settings,
            component="storage",
            incident_type=incident_type,
            fingerprint=fingerprint,
            severity="info",
            title="Storage recovered",
            detail=message,
        )
        health_storage.resolve_incident(
            settings,
            component="storage",
            incident_type=incident_type,
            fingerprint=fingerprint,
        )
    notifications.append((incident_id, alert_key, message))


def sync_storage_alerts(
    settings: Settings,
    overall: OverallHealth,
) -> list[tuple[int, str, str]]:
    """
    Emit one alert per actual storage state transition.
    Uses persisted watermark so restart does not replay recoveries.
    """
    notifications: list[tuple[int, str, str]] = []
    infra = next((c for c in overall.components if c.component == "infrastructure"), None)
    metrics = sanitize_mapping(infra.metrics if infra else {})
    level = str(metrics.get("storage_level") or "HEALTHY")
    if level not in {"HEALTHY", "DEGRADED", "CRITICAL"}:
        level = "HEALTHY"

    backup = backup_status(settings)
    backup_status_value = str(backup.status or "UNAVAILABLE")
    gate = revalidate_verified_backup_for_pruning(
        settings,
        max_age_seconds=settings.storage_retention_prune_max_backup_age_seconds,
    )
    retention_gate = "READY" if gate.ok else "BLOCKED"

    watermark = _load_watermark(settings)
    prev_level = str(watermark.get("storage_level") or "")
    prev_backup = str(watermark.get("backup_status") or "")
    prev_gate = str(watermark.get("retention_gate") or "")
    prev_backup_label = str(watermark.get("backup_label") or "")

    # First observation after restart/install: seed watermark silently (no replay).
    if not prev_level:
        _save_watermark(
            settings,
            {
                "storage_level": level,
                "backup_status": backup_status_value,
                "retention_gate": retention_gate,
                "backup_label": backup.backup_label or "",
                "backup_age_alerted_label": "",
                "cleanup_failure_key": "",
                "prune_failure_key": "",
                "seeded_at": _now_iso(),
            },
        )
        return notifications

    if prev_level != level:
        if prev_level == "HEALTHY" and level == "DEGRADED":
            _enqueue(
                settings,
                incident_type="storage_level",
                fingerprint="storage:level",
                severity="warning",
                title="Storage DEGRADED",
                detail=str(metrics.get("storage_detail") or "Storage degraded"),
                alert_key=f"storage:level:{prev_level}->{level}",
                message=f"Storage alert: HEALTHY → DEGRADED\n{metrics.get('storage_detail') or ''}".strip(),
                notifications=notifications,
            )
        elif prev_level in {"HEALTHY", "DEGRADED"} and level == "CRITICAL":
            _enqueue(
                settings,
                incident_type="storage_level",
                fingerprint="storage:level",
                severity="critical",
                title="Storage CRITICAL",
                detail=str(metrics.get("storage_detail") or "Storage critical"),
                alert_key=f"storage:level:{prev_level}->{level}",
                message=f"Storage alert: {prev_level} → CRITICAL\n{metrics.get('storage_detail') or ''}".strip(),
                notifications=notifications,
            )
        elif level == "HEALTHY" and prev_level in {"DEGRADED", "CRITICAL"}:
            _resolve_and_notify(
                settings,
                incident_type="storage_level",
                fingerprint="storage:level",
                alert_key=f"storage:level:{prev_level}->HEALTHY",
                message=f"Storage recovery: {prev_level} → HEALTHY",
                notifications=notifications,
            )

    if prev_backup == "VERIFIED" and backup_status_value == "FAILED":
        _enqueue(
            settings,
            incident_type="backup_status",
            fingerprint="storage:backup",
            severity="critical",
            title="Backup FAILED",
            detail=backup.detail or "backup failed",
            alert_key=f"storage:backup:VERIFIED->FAILED:{backup.backup_label or 'none'}",
            message=f"Storage alert: backup VERIFIED → FAILED\n{backup.detail}",
            notifications=notifications,
        )

    # Verified backup becoming too old (one alert per backup label).
    age_alerted = str(watermark.get("backup_age_alerted_label") or "")
    max_age = settings.storage_retention_prune_max_backup_age_seconds
    if (
        backup_status_value == "VERIFIED"
        and backup.age_seconds is not None
        and backup.age_seconds > max_age
        and (backup.backup_label or "") != age_alerted
    ):
        label = backup.backup_label or "(none)"
        _enqueue(
            settings,
            incident_type="backup_age",
            fingerprint=f"storage:backup_age:{label}",
            severity="warning",
            title="Verified backup too old",
            detail=f"age_seconds={int(backup.age_seconds)}",
            alert_key=f"storage:backup_age:{label}:{int(backup.age_seconds // max_age)}",
            message=(
                f"Storage alert: verified backup too old\n"
                f"Label: {label}\nAge seconds: {int(backup.age_seconds)}"
            ),
            notifications=notifications,
        )
        watermark["backup_age_alerted_label"] = label

    if prev_gate == "READY" and retention_gate == "BLOCKED":
        _enqueue(
            settings,
            incident_type="retention_gate",
            fingerprint="storage:retention_gate",
            severity="warning",
            title="Retention gate BLOCKED",
            detail=gate.detail or gate.failure_code or "blocked",
            alert_key=f"storage:gate:READY->BLOCKED:{gate.failure_code or 'blocked'}",
            message=(
                f"Storage alert: retention gate READY → BLOCKED\n"
                f"{gate.failure_code or gate.detail}"
            ),
            notifications=notifications,
        )

    cleanup = get_last_cleanup_report()
    if cleanup is not None and cleanup.status in {"failed", "lock_busy"} and cleanup.errors:
        key = f"cleanup:{cleanup.status}:{cleanup.checked_at}:{cleanup.errors}"
        if key != str(watermark.get("cleanup_failure_key") or ""):
            _enqueue(
                settings,
                incident_type="cleanup_failure",
                fingerprint="storage:cleanup_failure",
                severity="warning",
                title="File cleanup failure",
                detail=cleanup.detail or cleanup.status,
                alert_key=f"storage:cleanup_failure:{cleanup.checked_at}",
                message=f"Storage alert: file cleanup failure ({cleanup.status})",
                notifications=notifications,
            )
            watermark["cleanup_failure_key"] = key
    elif cleanup is not None and cleanup.status == "failed":
        key = f"cleanup:{cleanup.status}:{cleanup.checked_at}"
        if key != str(watermark.get("cleanup_failure_key") or ""):
            _enqueue(
                settings,
                incident_type="cleanup_failure",
                fingerprint="storage:cleanup_failure",
                severity="warning",
                title="File cleanup failure",
                detail=cleanup.detail or cleanup.status,
                alert_key=f"storage:cleanup_failure:{cleanup.checked_at}",
                message=f"Storage alert: file cleanup failure ({cleanup.status})",
                notifications=notifications,
            )
            watermark["cleanup_failure_key"] = key

    prune = get_last_prune_report()
    if prune is not None and prune.status == "FAILED":
        key = f"prune:{prune.failure_code}:{prune.finished_at}"
        if key != str(watermark.get("prune_failure_key") or ""):
            _enqueue(
                settings,
                incident_type="prune_failure",
                fingerprint="storage:prune_failure",
                severity="warning",
                title="Retention pruning failure",
                detail=prune.detail or prune.failure_code,
                alert_key=f"storage:prune_failure:{prune.finished_at}:{prune.failure_code}",
                message=(
                    f"Storage alert: retention pruning failure\n"
                    f"{prune.failure_code or prune.detail}"
                ),
                notifications=notifications,
            )
            watermark["prune_failure_key"] = key

    watermark.update(
        {
            "storage_level": level,
            "backup_status": backup_status_value,
            "retention_gate": retention_gate,
            "backup_label": backup.backup_label or prev_backup_label,
            "updated_at": _now_iso(),
        }
    )
    _save_watermark(settings, watermark)
    return notifications


def storage_recovery_covered(notifications: list[tuple[int, str, str]]) -> bool:
    """True when a storage-level recovery alert is already queued this tick."""
    return any(
        key.startswith("storage:level:") and "->HEALTHY" in key
        for _, key, _ in notifications
    )
