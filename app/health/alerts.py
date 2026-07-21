"""Deduplicated admin-only health incident alerts."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.models import ComponentStatus, OverallHealth
from app.health.sanitize import sanitize_mapping
from app.health.storage_alerts import storage_recovery_covered, sync_storage_alerts

logger = logging.getLogger(__name__)

SendFunc = Callable[[int, str], Awaitable[None]]


def _fingerprint(component: str, incident_type: str) -> str:
    return f"{component}:{incident_type}"


def sync_incidents_from_health(
    settings: Settings,
    overall: OverallHealth,
) -> list[tuple[int, str, str]]:
    """
    Open/update/resolve incidents from component statuses.
    Returns list of (incident_id, transition, message) for notifications.
    """
    notifications: list[tuple[int, str, str]] = []
    storage_notes = sync_storage_alerts(settings, overall)
    notifications.extend(storage_notes)
    storage_recovered = storage_recovery_covered(storage_notes)

    infra_recovered = False
    for comp in overall.components:
        if comp.status in {"DEGRADED", "DOWN"}:
            incident_type = "status_" + comp.status.lower()
            fp = _fingerprint(comp.component, incident_type)
            severity = "critical" if comp.status == "DOWN" else "warning"
            incident_id, transition = health_storage.open_or_update_incident(
                settings,
                component=comp.component,
                incident_type=incident_type,
                fingerprint=fp,
                severity=severity,
                title=f"{comp.component} is {comp.status}",
                detail=comp.detail,
            )
            if transition in {"open", "updated"}:
                # Dedup: only notify on open, or escalate to DOWN
                alert_key = f"{fp}:{transition}:{comp.status}"
                if transition == "updated" and comp.status != "DOWN":
                    continue
                if health_storage.was_alert_sent(settings, alert_key):
                    continue
                msg = (
                    f"Health alert: {comp.component} → {comp.status}\n"
                    f"{comp.detail}"
                )
                notifications.append((incident_id, alert_key, msg))
        elif comp.status == "HEALTHY":
            for incident_type in ("status_degraded", "status_down"):
                fp = _fingerprint(comp.component, incident_type)
                incident_id = health_storage.resolve_incident(
                    settings,
                    component=comp.component,
                    incident_type=incident_type,
                    fingerprint=fp,
                )
                if incident_id is not None:
                    alert_key = f"{fp}:resolved"
                    if health_storage.was_alert_sent(settings, alert_key):
                        continue
                    # Suppress duplicate infrastructure recovery when storage
                    # recovery already covers the same underlying transition.
                    if comp.component == "infrastructure" and storage_recovered:
                        health_storage.record_alert_sent(
                            settings,
                            incident_id=incident_id,
                            alert_key=alert_key,
                            payload=sanitize_mapping({"suppressed": "storage_recovery"}),
                        )
                        infra_recovered = True
                        continue
                    msg = f"Health recovery: {comp.component} is HEALTHY"
                    notifications.append((incident_id, alert_key, msg))
                    if comp.component == "infrastructure":
                        infra_recovered = True

    # Overall transitions
    if overall.status in {"DEGRADED", "DOWN"}:
        fp = _fingerprint("overall", "status_" + overall.status.lower())
        incident_id, transition = health_storage.open_or_update_incident(
            settings,
            component="overall",
            incident_type="status_" + overall.status.lower(),
            fingerprint=fp,
            severity="critical" if overall.status == "DOWN" else "warning",
            title=f"Overall health {overall.status}",
            detail=overall.summary,
        )
        if transition == "open":
            alert_key = f"{fp}:open"
            if not health_storage.was_alert_sent(settings, alert_key):
                notifications.append(
                    (
                        incident_id,
                        alert_key,
                        f"Overall health → {overall.status}\n{overall.summary}",
                    )
                )
    elif overall.status == "HEALTHY":
        for incident_type in ("status_degraded", "status_down"):
            fp = _fingerprint("overall", incident_type)
            incident_id = health_storage.resolve_incident(
                settings,
                component="overall",
                incident_type=incident_type,
                fingerprint=fp,
            )
            if incident_id is not None:
                alert_key = f"{fp}:resolved"
                if health_storage.was_alert_sent(settings, alert_key):
                    continue
                # Component/storage recovery already notified — do not duplicate.
                if infra_recovered or storage_recovered:
                    health_storage.record_alert_sent(
                        settings,
                        incident_id=incident_id,
                        alert_key=alert_key,
                        payload=sanitize_mapping({"suppressed": "component_recovery"}),
                    )
                    continue
                notifications.append(
                    (incident_id, alert_key, "Overall health recovered: HEALTHY")
                )

    return notifications


async def dispatch_health_alerts(
    settings: Settings,
    overall: OverallHealth,
    send: SendFunc,
) -> int:
    if not settings.health_alerts_enabled or not settings.admin_ids:
        return 0
    notes = sync_incidents_from_health(settings, overall)
    sent = 0
    for incident_id, alert_key, message in notes:
        if not health_storage.record_alert_sent(
            settings,
            incident_id=incident_id,
            alert_key=alert_key,
            payload=sanitize_mapping({"text": message}),
        ):
            continue
        for admin_id in settings.admin_ids:
            try:
                await send(admin_id, message)
                sent += 1
            except Exception:
                logger.exception("Failed health alert to admin")
    return sent


def maybe_alert_readonly_violation(settings: Settings) -> list[tuple[int, str, str]]:
    if settings.copy_intel_enabled and not settings.copy_intel_read_only:
        overall = OverallHealth(
            status="DOWN",
            components=(
                ComponentStatus(
                    component="copy_readonly",
                    status="DOWN",
                    detail="READ-ONLY SAFETY VIOLATION",
                    checked_at="",
                ),
            ),
            checked_at="",
            summary="Read-only safety violation",
        )
        return sync_incidents_from_health(settings, overall)
    return []
