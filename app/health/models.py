"""Health monitor domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

HealthStatus = Literal["HEALTHY", "DEGRADED", "DOWN", "DISABLED"]
IncidentState = Literal["open", "updated", "acknowledged", "suppressed", "resolved"]


@dataclass(frozen=True)
class ComponentStatus:
    component: str
    status: HealthStatus
    detail: str
    checked_at: str
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OverallHealth:
    status: HealthStatus
    components: tuple[ComponentStatus, ...]
    checked_at: str
    summary: str
