"""Durable storage transition alerts with restart-safe watermarks (Phase 5)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.diagnostics import format_hard_event_alert, format_recovery_alert
from app.health.models import ComponentStatus, OverallHealth
from app.health.sanitize import redact_string, sanitize_mapping
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


def _storage_message(
    settings: Settings,
    *,
    reason_code: str,
    what_happened_uk: str,
    incident_id: int,
    opened_at: str | None = None,
) -> str:
    return format_hard_event_alert(
        settings,
        component_id="storage",
        reason_code=reason_code,
        what_happened_uk=what_happened_uk,
        consecutive_failures=settings.health_failure_threshold,
        incident_id=incident_id,
        opened_at=opened_at or _now_iso(),
        severity_uk="Критично",
    )


def _storage_recovery_message(settings: Settings, *, incident_id: int) -> str:
    comp = ComponentStatus(
        component="storage",
        status="HEALTHY",
        detail="Storage recovered",
        checked_at=_now_iso(),
    )
    return format_recovery_alert(
        settings,
        comp,
        reason_code="storage_level",
        incident_id=incident_id,
        opened_at=None,
        resolved_at=_now_iso(),
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
    reason_code: str,
    what_happened_uk: str,
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
        pass
    message = _storage_message(
        settings,
        reason_code=reason_code,
        what_happened_uk=what_happened_uk,
        incident_id=incident_id,
    )
    notifications.append((incident_id, alert_key, message))


def _resolve_and_notify(
    settings: Settings,
    *,
    incident_type: str,
    fingerprint: str,
    alert_key: str,
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
        incident_id, _ = health_storage.open_or_update_incident(
            settings,
            component="storage",
            incident_type=incident_type,
            fingerprint=fingerprint,
            severity="info",
            title="Сховище відновлено",
            detail="storage recovered",
        )
        health_storage.resolve_incident(
            settings,
            component="storage",
            incident_type=incident_type,
            fingerprint=fingerprint,
        )
    message = _storage_recovery_message(settings, incident_id=incident_id)
    notifications.append((incident_id, alert_key, message))


def sync_storage_alerts(
    settings: Settings,
    overall: OverallHealth,
) -> list[tuple[int, str, str]]:
    """
    Emit one alert per actual storage state transition.
    Soft DEGRADED / cleanup delays / retention gate: watermark only (no Telegram).
    Hard CRITICAL / backup FAILED: structured Ukrainian admin alerts.
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
            # Soft storage DEGRADED while writes remain safe — UI only.
            logger.info("Storage level DEGRADED (Telegram suppressed)")
        elif prev_level in {"HEALTHY", "DEGRADED"} and level == "CRITICAL":
            detail = redact_string(str(metrics.get("storage_detail") or "critical"))
            _enqueue(
                settings,
                incident_type="storage_level",
                fingerprint="storage:level",
                severity="critical",
                title="Сховище критичне",
                detail=detail,
                alert_key=f"storage:level:{prev_level}->{level}",
                reason_code="storage_critical",
                what_happened_uk=f"Рівень сховища {prev_level} → CRITICAL",
                notifications=notifications,
            )
        elif level == "HEALTHY" and prev_level in {"DEGRADED", "CRITICAL"}:
            if prev_level == "CRITICAL":
                _resolve_and_notify(
                    settings,
                    incident_type="storage_level",
                    fingerprint="storage:level",
                    alert_key=f"storage:level:{prev_level}->HEALTHY",
                    notifications=notifications,
                )

    if prev_backup == "VERIFIED" and backup_status_value == "FAILED":
        _enqueue(
            settings,
            incident_type="backup_status",
            fingerprint="storage:backup",
            severity="critical",
            title="Backup FAILED",
            detail=redact_string(backup.detail or "backup failed"),
            alert_key=f"storage:backup:VERIFIED->FAILED:{backup.backup_label or 'none'}",
            reason_code="backup_failed",
            what_happened_uk="Перевірений backup перейшов у стан FAILED",
            notifications=notifications,
        )

    # Soft: verified backup age / retention gate / cleanup / prune — watermark only.
    age_alerted = str(watermark.get("backup_age_alerted_label") or "")
    max_age = settings.storage_retention_prune_max_backup_age_seconds
    if (
        backup_status_value == "VERIFIED"
        and backup.age_seconds is not None
        and backup.age_seconds > max_age
        and (backup.backup_label or "") != age_alerted
    ):
        label = backup.backup_label or "(none)"
        logger.info("Verified backup too old label=%s (Telegram suppressed)", label)
        watermark["backup_age_alerted_label"] = label

    if prev_gate == "READY" and retention_gate == "BLOCKED":
        logger.info(
            "Retention gate BLOCKED code=%s (Telegram suppressed)",
            gate.failure_code or "blocked",
        )

    cleanup = get_last_cleanup_report()
    if cleanup is not None and cleanup.status in {"failed", "lock_busy"}:
        key = f"cleanup:{cleanup.status}:{cleanup.checked_at}"
        if key != str(watermark.get("cleanup_failure_key") or ""):
            logger.info(
                "File cleanup status=%s (Telegram suppressed)", cleanup.status
            )
            watermark["cleanup_failure_key"] = key

    prune = get_last_prune_report()
    if prune is not None and prune.status == "FAILED":
        key = f"prune:{prune.failure_code}:{prune.finished_at}"
        if key != str(watermark.get("prune_failure_key") or ""):
            logger.info(
                "Retention prune FAILED code=%s (Telegram suppressed)",
                prune.failure_code,
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
