"""Overall health aggregation."""

from __future__ import annotations

from datetime import datetime, timezone

from app.health.models import ComponentStatus, HealthStatus, OverallHealth
from app.health.registry import label_uk_for

CRITICAL_COMPONENTS = frozenset(
    {"telegram", "sqlite", "copy_readonly"}
)


def aggregate_overall(components: list[ComponentStatus]) -> OverallHealth:
    checked_at = datetime.now(timezone.utc).isoformat()
    active = [
        c
        for c in components
        if c.status not in {"DISABLED", "WARMING_UP", "UNKNOWN"}
    ]
    # WARMING_UP/UNKNOWN do not drive overall failure alone.
    warming_only = [c for c in components if c.status in {"WARMING_UP", "UNKNOWN"}]
    if not active and not warming_only:
        disabled = [c for c in components if c.status == "DISABLED"]
        if disabled and len(disabled) == len(components):
            return OverallHealth(
                status="DISABLED",
                components=tuple(components),
                checked_at=checked_at,
                summary="Усі компоненти вимкнено",
            )
        return OverallHealth(
            status="UNKNOWN",
            components=tuple(components),
            checked_at=checked_at,
            summary="Недостатньо діагностичних даних",
        )
    if not active and warming_only:
        return OverallHealth(
            status="WARMING_UP",
            components=tuple(components),
            checked_at=checked_at,
            summary="Очікування першої справедливої перевірки",
        )

    for comp in active:
        if comp.component in CRITICAL_COMPONENTS and comp.status == "DOWN":
            return OverallHealth(
                status="DOWN",
                components=tuple(components),
                checked_at=checked_at,
                summary=f"Критична можливість недоступна: {label_uk_for(comp.component)}",
            )

    if any(c.status == "DOWN" for c in active):
        down = [label_uk_for(c.component) for c in active if c.status == "DOWN"]
        return OverallHealth(
            status="DEGRADED",
            components=tuple(components),
            checked_at=checked_at,
            summary=f"Некритичні збої: {', '.join(down)}",
        )

    if any(c.status == "DEGRADED" for c in active):
        deg = [label_uk_for(c.component) for c in active if c.status == "DEGRADED"]
        return OverallHealth(
            status="DEGRADED",
            components=tuple(components),
            checked_at=checked_at,
            summary=f"Обмежено: {', '.join(deg)}",
        )

    return OverallHealth(
        status="HEALTHY",
        components=tuple(components),
        checked_at=checked_at,
        summary="Усі активні компоненти працюють",
    )


def status_rank(status: HealthStatus) -> int:
    return {
        "HEALTHY": 0,
        "DISABLED": 1,
        "WARMING_UP": 1,
        "UNKNOWN": 2,
        "DEGRADED": 3,
        "DOWN": 4,
    }.get(status, 2)
