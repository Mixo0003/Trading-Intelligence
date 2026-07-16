"""Per-scan request cache, Weather.com circuit breaker, and concurrency stats."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class ScanRequestState:
    """Mutable per-scan state shared across discovery, expansion, and forecasts."""

    weather_com_disabled: bool = False
    weather_com_401_count: int = 0
    weather_com_request_count: int = 0
    forecast_cache: dict[tuple[Any, ...], Any] = field(default_factory=dict)
    event_by_id_cache: dict[str, Any] = field(default_factory=dict)
    event_by_slug_cache: dict[str, Any] = field(default_factory=dict)
    expansion_cache: dict[str, Any] = field(default_factory=dict)
    station_cache: dict[str, Any] = field(default_factory=dict)
    price_cache: dict[str, Any] = field(default_factory=dict)
    active_workers: int = 0
    max_concurrency_observed: int = 0
    polymarket_429_count: int = 0


_active_scan_state: ContextVar[ScanRequestState | None] = ContextVar(
    "scan_request_state",
    default=None,
)


def active_scan_state() -> ScanRequestState | None:
    return _active_scan_state.get()


def reset_scan_state() -> ScanRequestState:
    state = ScanRequestState()
    _active_scan_state.set(state)
    return state


def clear_scan_state() -> None:
    _active_scan_state.set(None)


@contextmanager
def scan_state_scope() -> Iterator[ScanRequestState]:
    state = reset_scan_state()
    try:
        yield state
    finally:
        clear_scan_state()


def note_worker_enter(state: ScanRequestState | None = None) -> None:
    current = state or active_scan_state()
    if current is None:
        return
    current.active_workers += 1
    if current.active_workers > current.max_concurrency_observed:
        current.max_concurrency_observed = current.active_workers


def note_worker_exit(state: ScanRequestState | None = None) -> None:
    current = state or active_scan_state()
    if current is None:
        return
    current.active_workers = max(0, current.active_workers - 1)


def mark_weather_com_unauthorized() -> None:
    state = active_scan_state()
    if state is None:
        return
    state.weather_com_disabled = True
    state.weather_com_401_count += 1


def weather_com_is_disabled() -> bool:
    state = active_scan_state()
    return bool(state and state.weather_com_disabled)


def note_weather_com_request() -> None:
    state = active_scan_state()
    if state is not None:
        state.weather_com_request_count += 1


def note_polymarket_429() -> None:
    state = active_scan_state()
    if state is not None:
        state.polymarket_429_count += 1
