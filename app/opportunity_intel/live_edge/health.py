"""Provider health diagnostics. Never invents live events."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class ProviderHealthStatus(StrEnum):
    WORKING = "WORKING"
    DEGRADED = "DEGRADED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    BLOCKED = "BLOCKED"
    OFFLINE = "OFFLINE"


@dataclass
class ProviderHealth:
    name: str
    status: ProviderHealthStatus = ProviderHealthStatus.OFFLINE
    last_success: datetime | None = None
    last_error: str | None = None
    live_events_found: int = 0
    named_events: int = 0
    matched_polymarket_events: int = 0
    data_age_seconds: float | None = None
    last_status_code: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_REGISTRY: dict[str, ProviderHealth] = {}


def clear_provider_health() -> None:
    _REGISTRY.clear()


def record_http(
    name: str,
    *,
    status_code: int | None,
    error: str | None,
    now: datetime | None = None,
) -> None:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = _REGISTRY.get(name) or ProviderHealth(name=name)
    row.last_status_code = status_code
    if error:
        row.last_error = error
        row.status = _status_from_error(status_code, error)
        _REGISTRY[name] = row
        return
    row.status = ProviderHealthStatus.WORKING
    row.last_success = clock
    row.last_error = None
    _REGISTRY[name] = row


def record_auth_required(name: str, reason: str) -> None:
    row = _REGISTRY.get(name) or ProviderHealth(name=name)
    row.status = ProviderHealthStatus.AUTH_REQUIRED
    row.last_error = reason
    _REGISTRY[name] = row


def record_snapshot_counts(
    name: str,
    *,
    live_events: int,
    named_events: int,
    data_age_seconds: float | None = None,
) -> None:
    row = _REGISTRY.get(name) or ProviderHealth(name=name)
    row.live_events_found = live_events
    row.named_events = named_events
    row.data_age_seconds = data_age_seconds
    if named_events == 0 and row.status == ProviderHealthStatus.WORKING:
        row.status = ProviderHealthStatus.DEGRADED
    _REGISTRY[name] = row


def record_matched_polymarket(name: str, count: int) -> None:
    row = _REGISTRY.get(name) or ProviderHealth(name=name)
    row.matched_polymarket_events = count
    _REGISTRY[name] = row


def get_provider_health(name: str) -> ProviderHealth | None:
    row = _REGISTRY.get(name)
    return None if row is None else replace(row, extra=dict(row.extra))


def list_provider_health() -> tuple[ProviderHealth, ...]:
    return tuple(replace(row, extra=dict(row.extra)) for row in _REGISTRY.values())


def _status_from_error(status_code: int | None, error: str) -> ProviderHealthStatus:
    text = error.lower()
    if status_code == 429 or "rate" in text:
        return ProviderHealthStatus.RATE_LIMITED
    if status_code in {401, 403} and ("token" in text or "missing" in text or "auth" in text):
        return ProviderHealthStatus.AUTH_REQUIRED
    if status_code == 403:
        return ProviderHealthStatus.BLOCKED
    if status_code == 401:
        return ProviderHealthStatus.AUTH_REQUIRED
    if "timeout" in text or "connection" in text or status_code is None:
        return ProviderHealthStatus.OFFLINE
    if status_code is not None and status_code >= 400:
        return ProviderHealthStatus.DEGRADED
    return ProviderHealthStatus.OFFLINE
