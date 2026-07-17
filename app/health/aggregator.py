"""Overall health aggregation."""

from __future__ import annotations

from datetime import datetime, timezone

from app.health.models import ComponentStatus, HealthStatus, OverallHealth

CRITICAL_COMPONENTS = frozenset(
    {"telegram", "sqlite", "copy_readonly"}
)


def aggregate_overall(components: list[ComponentStatus]) -> OverallHealth:
    checked_at = datetime.now(timezone.utc).isoformat()
    active = [c for c in components if c.status != "DISABLED"]
    if not active:
        return OverallHealth(
            status="DISABLED",
            components=tuple(components),
            checked_at=checked_at,
            summary="All components disabled",
        )

    for comp in active:
        if comp.component in CRITICAL_COMPONENTS and comp.status == "DOWN":
            return OverallHealth(
                status="DOWN",
                components=tuple(components),
                checked_at=checked_at,
                summary=f"Critical component down: {comp.component}",
            )

    if any(c.status == "DOWN" for c in active):
        down = [c.component for c in active if c.status == "DOWN"]
        return OverallHealth(
            status="DEGRADED",
            components=tuple(components),
            checked_at=checked_at,
            summary=f"Non-critical down: {', '.join(down)}",
        )

    if any(c.status == "DEGRADED" for c in active):
        deg = [c.component for c in active if c.status == "DEGRADED"]
        return OverallHealth(
            status="DEGRADED",
            components=tuple(components),
            checked_at=checked_at,
            summary=f"Degraded: {', '.join(deg)}",
        )

    return OverallHealth(
        status="HEALTHY",
        components=tuple(components),
        checked_at=checked_at,
        summary="All monitored components healthy",
    )


def status_rank(status: HealthStatus) -> int:
    return {"HEALTHY": 0, "DISABLED": 1, "DEGRADED": 2, "DOWN": 3}[status]
