"""Deduplicated admin-only health incident alerts (global policy)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.diagnostics import format_down_alert, format_recovery_alert
from app.health.incident_keys import (
    incident_fingerprint,
    normalize_reason_code,
    notified_open_marker_key,
    open_alert_key,
    recovery_alert_key,
)
from app.health.models import ComponentStatus, OverallHealth
from app.health.policy import (
    decide_open_notification,
    may_alert_overall,
    reason_code_for,
    soft_only_overall,
)
from app.health.sanitize import sanitize_mapping
from app.health.storage_alerts import storage_recovery_covered, sync_storage_alerts

logger = logging.getLogger(__name__)

SendFunc = Callable[[int, str], Awaitable[None]]

_MOI_EIA_FP = "market_opportunity:eia_price_down"


def _incident_opened_at(settings: Settings, incident_id: int) -> str | None:
    rows = health_storage.list_recent_incidents(settings, limit=50)
    for row in rows:
        if int(row["id"]) == incident_id:
            return str(row["opened_at"] or "") or None
    return None


def _consecutive_failures(settings: Settings, component: str) -> int:
    prior = health_storage.get_component_state(settings, component)
    if prior is None:
        return 0
    return int(prior["consecutive_failures"] or 0)


def _sync_moi_health_alerts(
    settings: Settings,
    comp: ComponentStatus,
) -> list[tuple[int, str, str]]:
    """MOI EIA path: soft FRED/warmup never Telegram; EIA DOWN after threshold."""
    notes: list[tuple[int, str, str]] = []
    metrics: dict[str, Any] = dict(comp.metrics or {})
    price = metrics.get("wti_price") if isinstance(metrics.get("wti_price"), dict) else {}
    price_state = str(price.get("state") or "")
    price_detail = str(price.get("detail") or comp.detail or "")

    if price_state == "HEALTHY":
        incident_id = health_storage.resolve_incident(
            settings,
            component="market_opportunity",
            incident_type="eia_price_down",
            fingerprint=_MOI_EIA_FP,
        )
        if incident_id is None:
            return notes
        marker = notified_open_marker_key(fingerprint=_MOI_EIA_FP, incident_id=incident_id)
        if not health_storage.was_alert_sent(settings, marker):
            # Soft resolve without recovery spam when never notified.
            health_storage.record_alert_sent(
                settings,
                incident_id=incident_id,
                alert_key=recovery_alert_key(fingerprint=_MOI_EIA_FP, incident_id=incident_id),
                payload=sanitize_mapping({"suppressed": "never_notified"}),
            )
            return notes
        alert_key = recovery_alert_key(fingerprint=_MOI_EIA_FP, incident_id=incident_id)
        if health_storage.was_alert_sent(settings, alert_key):
            return notes
        opened = _incident_opened_at(settings, incident_id)
        msg = format_recovery_alert(
            settings,
            comp,
            reason_code="eia_price_down",
            incident_id=incident_id,
            opened_at=opened,
            resolved_at=None,
        )
        # Stable ops cue expected by focused MOI recovery tests.
        msg = f"{msg}\nEIA RWTC is HEALTHY"
        notes.append((incident_id, alert_key, msg))
        return notes

    if metrics.get("warming") or metrics.get("telegram_alert_policy") == "suppress":
        return notes

    if price_state == "DOWN":
        failures = _consecutive_failures(settings, "market_opportunity")
        incident_id, transition = health_storage.open_or_update_incident(
            settings,
            component="market_opportunity",
            incident_type="eia_price_down",
            fingerprint=_MOI_EIA_FP,
            severity="critical",
            title="MOI EIA RWTC down",
            detail=price_detail,
        )
        if failures < settings.health_failure_threshold:
            return notes
        if transition not in {"open", "updated"}:
            return notes
        reason = "eia_price_down"
        alert_key = open_alert_key(
            fingerprint=_MOI_EIA_FP, incident_id=incident_id, reason_code=reason
        )
        if health_storage.was_alert_sent(settings, alert_key):
            return notes
        opened = _incident_opened_at(settings, incident_id)
        # Probe may still show DEGRADED with price DOWN — treat as feature DOWN alert.
        down_comp = ComponentStatus(
            component=comp.component,
            status="DOWN",
            detail=price_detail or "EIA RWTC missing",
            checked_at=comp.checked_at,
            metrics={**metrics, "reason_code": "eia_price_down"},
        )
        msg = format_down_alert(
            settings,
            down_comp,
            reason_code=reason,
            consecutive_failures=failures,
            incident_id=incident_id,
            opened_at=opened,
        )
        if "EIA RWTC missing" not in msg:
            msg = msg.replace(
                "Що сталося:",
                "Що сталося: EIA RWTC missing —",
                1,
            )
        notes.append((incident_id, alert_key, msg))
        # Mark notified immediately so recovery gating works without empty notes.
        health_storage.record_alert_sent(
            settings,
            incident_id=incident_id,
            alert_key=notified_open_marker_key(
                fingerprint=_MOI_EIA_FP, incident_id=incident_id
            ),
            payload=sanitize_mapping({"marker": True}),
        )
    return notes


def _handle_component(
    settings: Settings,
    comp: ComponentStatus,
    *,
    storage_recovered: bool,
) -> list[tuple[int, str, str]]:
    notes: list[tuple[int, str, str]] = []
    if comp.component == "market_opportunity":
        return _sync_moi_health_alerts(settings, comp)

    failures = _consecutive_failures(settings, comp.component)
    reason = reason_code_for(comp)
    fp = incident_fingerprint(comp.component, reason)

    if comp.status in {"DEGRADED", "DOWN"}:
        incident_type = "status_" + ("down" if comp.status == "DOWN" else "degraded")
        # Prefer stable reason fingerprint over status-only keys.
        fp = incident_fingerprint(comp.component, reason)
        severity = "critical" if comp.status == "DOWN" else "warning"
        from app.health.registry import label_uk_for

        incident_id, transition = health_storage.open_or_update_incident(
            settings,
            component=comp.component,
            incident_type=incident_type,
            fingerprint=fp,
            severity=severity,
            title=f"{label_uk_for(comp.component)} {comp.status}",
            detail=comp.detail,
        )
        decision = decide_open_notification(
            settings, comp, consecutive_failures=failures
        )
        if not decision.notify:
            return notes
        if transition not in {"open", "updated"}:
            return notes
        alert_key = open_alert_key(
            fingerprint=fp, incident_id=incident_id, reason_code=reason
        )
        if health_storage.was_alert_sent(settings, alert_key):
            return notes
        opened = _incident_opened_at(settings, incident_id)
        msg = format_down_alert(
            settings,
            comp,
            reason_code=reason,
            consecutive_failures=failures,
            incident_id=incident_id,
            opened_at=opened,
        )
        notes.append((incident_id, alert_key, msg))
        health_storage.record_alert_sent(
            settings,
            incident_id=incident_id,
            alert_key=notified_open_marker_key(fingerprint=fp, incident_id=incident_id),
            payload=sanitize_mapping({"marker": True}),
        )
        return notes

    if comp.status == "HEALTHY":
        from app.health.registry import get_component_spec

        spec = get_component_spec(comp.component)
        reason_candidates = {"status_degraded", "status_down"}
        if spec is not None:
            reason_candidates |= set(spec.reason_codes)
        # Prior open may have used the live reason_code metric.
        prior = health_storage.get_component_state(settings, comp.component)
        if prior is not None:
            try:
                import json

                raw = prior["metrics_json"] if "metrics_json" in prior.keys() else None
                if isinstance(raw, str) and raw.strip():
                    metrics = json.loads(raw)
                    if isinstance(metrics, dict) and metrics.get("reason_code"):
                        reason_candidates.add(str(metrics["reason_code"]))
            except Exception:
                pass
        for incident_type in ("status_degraded", "status_down"):
            for reason_key in sorted(reason_candidates):
                cand = incident_fingerprint(comp.component, reason_key)
                incident_id = health_storage.resolve_incident(
                    settings,
                    component=comp.component,
                    incident_type=incident_type,
                    fingerprint=cand,
                )
                if incident_id is None:
                    continue
                marker = notified_open_marker_key(
                    fingerprint=cand, incident_id=incident_id
                )
                alert_key = recovery_alert_key(fingerprint=cand, incident_id=incident_id)
                if health_storage.was_alert_sent(settings, alert_key):
                    continue
                if not health_storage.was_alert_sent(settings, marker):
                    health_storage.record_alert_sent(
                        settings,
                        incident_id=incident_id,
                        alert_key=alert_key,
                        payload=sanitize_mapping({"suppressed": "never_notified"}),
                    )
                    continue
                if comp.component == "infrastructure" and storage_recovered:
                    health_storage.record_alert_sent(
                        settings,
                        incident_id=incident_id,
                        alert_key=alert_key,
                        payload=sanitize_mapping({"suppressed": "storage_recovery"}),
                    )
                    continue
                opened = _incident_opened_at(settings, incident_id)
                msg = format_recovery_alert(
                    settings,
                    comp,
                    reason_code=normalize_reason_code(reason_key),
                    incident_id=incident_id,
                    opened_at=opened,
                    resolved_at=None,
                )
                notes.append((incident_id, alert_key, msg))
    return notes


def sync_incidents_from_health(
    settings: Settings,
    overall: OverallHealth,
) -> list[tuple[int, str, str]]:
    """
    Open/update/resolve incidents from component statuses.
    Returns list of (incident_id, alert_key, message) for notifications.
    Empty message entries are markers only and must not be sent.
    """
    notifications: list[tuple[int, str, str]] = []
    storage_notes = sync_storage_alerts(settings, overall)
    notifications.extend(storage_notes)
    storage_recovered = storage_recovery_covered(storage_notes)

    infra_recovered = False
    for comp in overall.components:
        notes = _handle_component(
            settings, comp, storage_recovered=storage_recovered
        )
        for item in notes:
            notifications.append(item)
            if (
                comp.component == "infrastructure"
                and item[2]
                and "Відновлення" in item[2]
            ):
                infra_recovered = True

    # Overall: soft-only DEGRADED never Telegram; prefer component alerts.
    if overall.status in {"DEGRADED", "DOWN"}:
        fp = incident_fingerprint("overall", "status_" + overall.status.lower())
        incident_id, transition = health_storage.open_or_update_incident(
            settings,
            component="overall",
            incident_type="status_" + overall.status.lower(),
            fingerprint=fp,
            severity="critical" if overall.status == "DOWN" else "warning",
            title=f"Overall health {overall.status}",
            detail=overall.summary,
        )
        if (
            transition == "open"
            and may_alert_overall(overall)
            and not soft_only_overall(overall)
        ):
            alert_key = open_alert_key(
                fingerprint=fp, incident_id=incident_id, reason_code=overall.status.lower()
            )
            if not health_storage.was_alert_sent(settings, alert_key):
                # Intentionally unused under may_alert_overall()==False default.
                notifications.append(
                    (
                        incident_id,
                        alert_key,
                        f"Загальний стан → {overall.status}\n{overall.summary}",
                    )
                )
    elif overall.status == "HEALTHY":
        for incident_type in ("status_degraded", "status_down"):
            fp = incident_fingerprint("overall", incident_type)
            incident_id = health_storage.resolve_incident(
                settings,
                component="overall",
                incident_type=incident_type,
                fingerprint=fp,
            )
            if incident_id is None:
                continue
            alert_key = recovery_alert_key(fingerprint=fp, incident_id=incident_id)
            if health_storage.was_alert_sent(settings, alert_key):
                continue
            if infra_recovered or storage_recovered:
                health_storage.record_alert_sent(
                    settings,
                    incident_id=incident_id,
                    alert_key=alert_key,
                    payload=sanitize_mapping({"suppressed": "component_recovery"}),
                )
                continue
            # Overall recovery suppressed to avoid duplicate spam with component recovery.
            health_storage.record_alert_sent(
                settings,
                incident_id=incident_id,
                alert_key=alert_key,
                payload=sanitize_mapping({"suppressed": "prefer_component_recovery"}),
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
        if not message:
            # Marker-only keys (notified_open) — record without Telegram send.
            health_storage.record_alert_sent(
                settings,
                incident_id=incident_id,
                alert_key=alert_key,
                payload=sanitize_mapping({"marker": True}),
            )
            continue
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
                    metrics={"reason_code": "readonly_violation"},
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
                    metrics={"reason_code": "readonly_violation"},
                ),
            ),
            checked_at="",
            summary="Crypto read-only safety violation",
        )
        return sync_incidents_from_health(settings, overall)
    return []
