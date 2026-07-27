"""Sanitized scanner status for Telegram users."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config.settings import Settings
from app.database import opportunity_storage
from app.opportunity_intel.models import ScannerStatus
from app.opportunity_intel.settings import NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS

STALE_CYCLE_MULTIPLIER = 30


@dataclass(frozen=True)
class ScannerStatusView:
    label: str
    last_complete_cycle_at: datetime | None
    cache_age_seconds: int | None
    baseline_label: str
    active_opportunity_count: int
    user_delivery_globally_available: bool
    text: str


def build_scanner_status_view(
    settings: Settings, *, now: datetime | None = None
) -> ScannerStatusView:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not settings.opportunity_scanner_enabled:
        return ScannerStatusView(
            label="Disabled",
            last_complete_cycle_at=None,
            cache_age_seconds=None,
            baseline_label="Not started",
            active_opportunity_count=0,
            user_delivery_globally_available=False,
            text="\n".join(
                [
                    "Scanner Status",
                    "",
                    "State: Disabled",
                    "Baseline: Not started",
                    "Active opportunities: 0",
                    "Automatic user delivery: not available globally",
                    "",
                    "This view uses persisted scanner state only.",
                    "It does not place trades or refresh market data.",
                ]
            ),
        )

    state = opportunity_storage.load_scanner_state(settings)
    gates = opportunity_storage.load_delivery_gates(settings)
    active_count = opportunity_storage.count_active_signals(settings)

    if state is None:
        label = "Not initialized"
        baseline = "Unknown"
        age = None
        last = None
    else:
        last = state.last_complete_cycle_at
        age = (
            int((clock - last.astimezone(timezone.utc)).total_seconds())
            if last is not None
            else None
        )
        if state.status == ScannerStatus.DISABLED:
            label = "Disabled"
        elif state.status == ScannerStatus.DOWN:
            label = "Unavailable"
        elif state.status == ScannerStatus.DEGRADED:
            label = "Degraded"
        elif state.status == ScannerStatus.BASELINING:
            label = "Baselining"
        elif last is None:
            label = "Not initialized"
        elif age is not None and age > NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS * STALE_CYCLE_MULTIPLIER:
            label = "Stale"
        elif state.status == ScannerStatus.HEALTHY:
            label = "Ready"
        else:
            label = "Unavailable"
        if state.global_baseline_cycles >= 2 and state.discovery_complete:
            baseline = "Ready"
        elif state.status == ScannerStatus.BASELINING or state.global_baseline_cycles < 2:
            baseline = "In progress"
        else:
            baseline = "Incomplete"

    user_gate = bool(gates.user_delivery_enabled) or bool(
        settings.opportunity_user_delivery_enabled
    )
    lines = [
        "Scanner Status",
        "",
        f"State: {label}",
        f"Baseline: {baseline}",
        f"Active opportunities: {active_count}",
        f"Automatic user delivery: {'available' if user_gate else 'not available globally'}",
    ]
    if last is not None:
        lines.append(f"Last complete cycle (UTC): {last.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    if age is not None:
        lines.append(f"Cache age: {age}s")
    lines.extend(
        [
            "",
            "This view uses persisted scanner state only.",
            "It does not place trades or refresh market data.",
        ]
    )
    return ScannerStatusView(
        label=label,
        last_complete_cycle_at=last,
        cache_age_seconds=age,
        baseline_label=baseline,
        active_opportunity_count=active_count,
        user_delivery_globally_available=user_gate,
        text="\n".join(lines),
    )
