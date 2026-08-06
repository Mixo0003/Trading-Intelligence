"""Telegram notification policy for the global health framework."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config.settings import Settings
from app.health.models import ComponentStatus, OverallHealth
from app.health.registry import get_component_spec

Decision = Literal["suppress", "alert_down", "alert_recovery", "record_only"]


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason_code: str
    notify: bool


def _reason_from_metrics(comp: ComponentStatus, default: str) -> str:
    metrics = comp.metrics or {}
    raw = metrics.get("reason_code") or metrics.get("diagnostic_reason")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:64]
    # MOI soft/hard cues
    if comp.component == "market_opportunity":
        if metrics.get("warming") or metrics.get("telegram_alert_policy") == "suppress":
            return "fred_optional" if "FRED" in (comp.detail or "") else "warming"
        price = metrics.get("wti_price") if isinstance(metrics.get("wti_price"), dict) else {}
        if str(price.get("state") or "") == "DOWN":
            return "eia_price_down"
        if str(price.get("state") or "") == "HEALTHY" and comp.status == "DEGRADED":
            return "fred_optional"
    detail = (comp.detail or "").lower()
    if "conflict" in detail:
        return "poller_conflict"
    if "read-only" in detail or "readonly" in detail:
        return "readonly_violation"
    if "integrity" in detail:
        return "integrity_failed"
    if "zero" in detail and "book" in detail:
        return "zero_valid_books"
    if "book" in detail and "degraded" in detail:
        return "reduced_book_coverage"
    return default


def reason_code_for(comp: ComponentStatus) -> str:
    if comp.status == "DOWN":
        return _reason_from_metrics(comp, "status_down")
    if comp.status == "DEGRADED":
        return _reason_from_metrics(comp, "status_degraded")
    if comp.status == "DISABLED":
        return "disabled"
    if comp.status == "WARMING_UP":
        return "warming"
    if comp.status == "UNKNOWN":
        return "unknown"
    return "healthy"


def should_alert_component_down(
    settings: Settings,
    comp: ComponentStatus,
    *,
    consecutive_failures: int,
) -> bool:
    """True when a DOWN component may emit a Telegram alert."""
    if comp.status != "DOWN":
        return False
    spec = get_component_spec(comp.component)
    if spec is None:
        return False
    if spec.alert_policy == "never":
        return False
    if spec.criticality == "optional":
        return False
    # MOI: soft suppress while FRED/warmup even if outer status looks bad
    metrics = comp.metrics or {}
    if comp.component == "market_opportunity":
        if metrics.get("warming") or metrics.get("telegram_alert_policy") == "suppress":
            return False
        price = metrics.get("wti_price") if isinstance(metrics.get("wti_price"), dict) else {}
        if str(price.get("state") or "") != "DOWN":
            # Outer DOWN without EIA price DOWN is not a MOI Telegram event.
            if comp.status == "DOWN" and str(price.get("state") or "") != "DOWN":
                return consecutive_failures >= settings.health_failure_threshold
            return False
    if spec.alert_policy == "immediate_hard_down" and spec.criticality == "hard_critical":
        return True
    return consecutive_failures >= settings.health_failure_threshold


def soft_only_overall(overall: OverallHealth) -> bool:
    """True when no registered hard/feature component is confirmed DOWN."""
    for comp in overall.components:
        if comp.status != "DOWN":
            continue
        spec = get_component_spec(comp.component)
        if spec is None:
            continue
        if spec.criticality in {"hard_critical", "feature_critical"} and spec.alert_policy != "never":
            return False
    return True


def may_alert_overall(overall: OverallHealth) -> bool:
    if overall.status != "DOWN" and not (
        overall.status == "DEGRADED"
        and any(c.status == "DOWN" for c in overall.components)
    ):
        # Soft-only DEGRADED overall → no Telegram
        if overall.status == "DEGRADED" and soft_only_overall(overall):
            return False
        if overall.status not in {"DOWN", "DEGRADED"}:
            return False
    if soft_only_overall(overall):
        return False
    # Prefer component-specific alerts: allow overall only when status is DOWN
    # for a hard-critical aggregate, but default suppress to avoid duplicates.
    return False


def decide_open_notification(
    settings: Settings,
    comp: ComponentStatus,
    *,
    consecutive_failures: int,
) -> PolicyDecision:
    code = reason_code_for(comp)
    if comp.status in {"HEALTHY", "DISABLED", "WARMING_UP", "UNKNOWN"}:
        return PolicyDecision("suppress", code, False)
    if comp.status == "DEGRADED":
        return PolicyDecision("record_only", code, False)
    if comp.status == "DOWN":
        if should_alert_component_down(
            settings, comp, consecutive_failures=consecutive_failures
        ):
            return PolicyDecision("alert_down", code, True)
        return PolicyDecision("record_only", code, False)
    return PolicyDecision("suppress", code, False)
