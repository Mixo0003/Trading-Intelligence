"""Deduplicated admin-only health incident alerts."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Awaitable, Callable

from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.models import ComponentStatus, OverallHealth
from app.health.sanitize import sanitize_mapping
from app.health.storage_alerts import storage_recovery_covered, sync_storage_alerts

logger = logging.getLogger(__name__)

SendFunc = Callable[[int, str], Awaitable[None]]

_MOI_EIA_FP = "market_opportunity:eia_price"


def _fingerprint(component: str, incident_type: str) -> str:
    return f"{component}:{incident_type}"


def _detail_fp(detail: str) -> str:
    return hashlib.sha256((detail or "").encode("utf-8")).hexdigest()[:12]


def _sync_moi_health_alerts(
    settings: Settings,
    comp: ComponentStatus,
) -> list[tuple[int, str, str]]:
    """Telegram policy for Market Opportunity Intelligence.

    System Health may still show DEGRADED for optional FRED / warmup.
    Telegram alerts are reserved for confirmed EIA price failures (after the
    health failure threshold) and a single EIA recovery notice.
    ``MARKET_OPPORTUNITY_AUTO_ALERTS_ENABLED`` does not gate these health
    alerts — ``HEALTH_ALERTS_ENABLED`` does via ``dispatch_health_alerts``.
    """
    notes: list[tuple[int, str, str]] = []
    metrics: dict[str, Any] = dict(comp.metrics or {})
    price = metrics.get("wti_price") if isinstance(metrics.get("wti_price"), dict) else {}
    price_state = str(price.get("state") or "")
    price_detail = str(price.get("detail") or comp.detail or "")

    # Recovery must run even when overall MOI remains soft-DEGRADED (FRED).
    if price_state == "HEALTHY":
        incident_id = health_storage.resolve_incident(
            settings,
            component="market_opportunity",
            incident_type="eia_price_down",
            fingerprint=_MOI_EIA_FP,
        )
        if incident_id is None:
            return notes
        alert_key = f"{_MOI_EIA_FP}:resolved:{incident_id}"
        if health_storage.was_alert_sent(settings, alert_key):
            return notes
        notes.append(
            (
                incident_id,
                alert_key,
                "Health recovery: market_opportunity EIA RWTC is HEALTHY",
            )
        )
        return notes

    if metrics.get("warming") or metrics.get("telegram_alert_policy") == "suppress":
        return notes

    if price_state == "DOWN":
        prior = health_storage.get_component_state(settings, "market_opportunity")
        failures = int((prior["consecutive_failures"] if prior is not None else 0) or 0)
        incident_id, _transition = health_storage.open_or_update_incident(
            settings,
            component="market_opportunity",
            incident_type="eia_price_down",
            fingerprint=_MOI_EIA_FP,
            severity="critical",
            title="market_opportunity EIA RWTC down",
            detail=price_detail,
        )
        if failures < settings.health_failure_threshold:
            return notes
        alert_key = f"{_MOI_EIA_FP}:alert:{incident_id}:{_detail_fp(price_detail)}"
        if health_storage.was_alert_sent(settings, alert_key):
            return notes
        notes.append(
            (
                incident_id,
                alert_key,
                f"Health alert: market_opportunity → {comp.status}\n{price_detail}",
            )
        )
    return notes


def _overall_alert_suppressed(overall: OverallHealth) -> bool:
    """Skip overall Telegram noise when only MOI is unhealthy.

    MOI uses a dedicated EIA alert/recovery path; optional FRED DEGRADED and
    warmup must not produce Overall health → DEGRADED Telegram spam.
    """
    bad = [c for c in overall.components if c.status in {"DEGRADED", "DOWN"}]
    if not bad:
        return True
    return all(c.component == "market_opportunity" for c in bad)


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
        if comp.component == "market_opportunity":
            notifications.extend(_sync_moi_health_alerts(settings, comp))
            continue

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
            if transition != "open":
                # Never notify on mere incident updates — previously
                # ``updated``+DOWN used a new alert_key and re-spammed admins.
                continue
            alert_key = (
                f"{fp}:open:{comp.status}:{_detail_fp(comp.detail)}"
            )
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
                    alert_key = f"{fp}:resolved:{incident_id}"
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
        if transition == "open" and not _overall_alert_suppressed(overall):
            alert_key = f"{fp}:open:{_detail_fp(overall.summary)}"
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
                alert_key = f"{fp}:resolved:{incident_id}"
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
    if settings.crypto_intel_enabled and not settings.crypto_intel_read_only:
        overall = OverallHealth(
            status="DOWN",
            components=(
                ComponentStatus(
                    component="crypto_readonly",
                    status="DOWN",
                    detail="READ-ONLY SAFETY VIOLATION",
                    checked_at="",
                ),
            ),
            checked_at="",
            summary="Crypto read-only safety violation",
        )
        return sync_incidents_from_health(settings, overall)
    return []
