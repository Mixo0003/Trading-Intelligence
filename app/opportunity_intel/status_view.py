"""Sanitized scanner status for Telegram users."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config.settings import Settings
from app.database import opportunity_storage
from app.opportunity_intel.display_labels import live_mode_label
from app.opportunity_intel.models import LiveMode, ScannerStatus
from app.opportunity_intel.scanner_control import auto_alerts_armed, scanner_armed
from app.opportunity_intel.settings import NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS

STALE_CYCLE_MULTIPLIER = 30

_DISCOVERY_LABELS = {
    "NOT_STARTED": "not started",
    "IN_PROGRESS": "in progress",
    "COMPLETE": "complete",
    "RETRYABLE_FAILURE": "retrying",
    "PERMANENT_FAILURE": "failed",
    "EXPIRED": "expired",
}


@dataclass(frozen=True)
class ScannerStatusView:
    label: str
    last_complete_cycle_at: datetime | None
    cache_age_seconds: int | None
    baseline_label: str
    active_opportunity_count: int
    user_delivery_globally_available: bool
    text: str
    scanner_enabled: bool = False
    discovery_label: str = "not started"
    discovery_complete: bool = False
    markets_accumulated: int = 0
    tracked_market_count: int = 0
    baseline_ready_count: int = 0
    progress_age_seconds: int | None = None
    auto_alerts_enabled: bool = False
    admin_delivery_available: bool = False
    admin_configuration_present: bool = False


def _age_seconds(value: datetime | None, *, clock: datetime) -> int | None:
    if value is None:
        return None
    return int((clock - value.astimezone(timezone.utc)).total_seconds())


def build_scanner_status_view(
    settings: Settings,
    *,
    now: datetime | None = None,
    viewer_live_mode: LiveMode | None = None,
) -> ScannerStatusView:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    admin_ids = tuple(settings.admin_ids or ())
    admin_configuration_present = len(admin_ids) > 0
    gates = opportunity_storage.load_delivery_gates(settings)
    user_gate = bool(gates.user_delivery_enabled) or bool(
        settings.opportunity_user_delivery_enabled
    )
    admin_gate = bool(gates.admin_delivery_enabled)
    admin_delivery_available = admin_configuration_present and admin_gate
    armed = scanner_armed(settings)
    auto_alerts = auto_alerts_armed(settings)
    viewer_off = viewer_live_mode == LiveMode.OFF

    # One source of truth: unarmed OR viewer Live Mode OFF → disabled/stopped.
    if not armed or viewer_off:
        lines = [
            "Scanner Status",
            "",
            "Scanner: disabled",
            "Scanner worker: stopped",
            "State: Disabled",
            "Discovery: not started",
            "Baseline: Not started",
            "Accumulated markets: 0",
            "Baseline-ready markets: 0",
            "Qualifying opportunities: 0",
            "Automatic alerts: off",
            "Automatic delivery to regular users: disabled",
            (
                "Administrator delivery: "
                f"{'available' if admin_delivery_available else 'unavailable'}"
            ),
        ]
        if viewer_live_mode is not None:
            lines.append(f"Live Mode: {live_mode_label(viewer_live_mode)}")
        lines.extend(
            [
                "",
                "Live Mode and Scanner Status use the same arming state.",
                "This view does not place trades or refresh market data.",
            ]
        )
        return ScannerStatusView(
            label="Disabled",
            last_complete_cycle_at=None,
            cache_age_seconds=None,
            baseline_label="Not started",
            active_opportunity_count=0,
            user_delivery_globally_available=user_gate,
            text="\n".join(lines),
            scanner_enabled=False,
            auto_alerts_enabled=False,
            admin_delivery_available=admin_delivery_available,
            admin_configuration_present=admin_configuration_present,
        )

    state = opportunity_storage.load_scanner_state(settings)
    active_count = opportunity_storage.count_active_signals(settings)
    progress = opportunity_storage.load_discovery_progress(settings)
    baseline_ready = opportunity_storage.count_baseline_ready_assets(settings)
    tracked_markets = opportunity_storage.count_market_states(settings)

    discovery_complete = bool(
        state is not None
        and state.discovery_complete
        and state.last_complete_discovery_at is not None
    )
    if progress is not None:
        discovery_label = _DISCOVERY_LABELS.get(progress.state, "in progress")
        markets_accumulated = progress.markets_accumulated
    elif discovery_complete:
        discovery_label = "complete"
        markets_accumulated = tracked_markets
    else:
        discovery_label = "not started"
        markets_accumulated = 0
    progress_age = _age_seconds(
        progress.updated_at if progress is not None else None, clock=clock
    )

    last = state.last_complete_cycle_at if state is not None else None
    age = _age_seconds(last, clock=clock)

    if state is None:
        label = "Baselining" if progress is not None else "Not initialized"
        baseline = "Discovery in progress" if progress is not None else "Unknown"
    else:
        stale_threshold = (
            NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS * STALE_CYCLE_MULTIPLIER
        )
        stale = age is not None and age > stale_threshold
        # Fresh discovery progress means rediscovery/soft-refresh is alive even
        # when last_complete_cycle_at is old — do not mislabel that as a dead worker.
        discovery_progress_fresh = (
            progress_age is not None
            and progress_age
            <= max(120, int(settings.opportunity_scan_interval_seconds) * 3)
            and discovery_label in {"in progress", "retrying"}
        )
        baseline_ready_evidence = discovery_complete and baseline_ready > 0
        if state.status == ScannerStatus.DISABLED:
            label = "Disabled"
        elif state.status == ScannerStatus.DOWN:
            label = "Unavailable"
        elif state.status == ScannerStatus.DEGRADED:
            label = "Degraded"
        elif last is None:
            label = "Not initialized"
        elif stale and discovery_progress_fresh:
            label = "Discovering"
        elif state.status == ScannerStatus.BASELINING or not baseline_ready_evidence:
            label = "Baselining"
        elif stale:
            label = "Stale"
        elif state.status == ScannerStatus.HEALTHY:
            label = "Ready"
        else:
            label = "Unavailable"
        if baseline_ready_evidence and state.status == ScannerStatus.HEALTHY:
            baseline = "Ready"
        elif not discovery_complete:
            baseline = "Waiting for complete discovery"
        else:
            baseline = "In progress"

    if label == "Disabled":
        worker_display = "stopped"
    else:
        worker_display = label

    lines = [
        "Scanner Status",
        "",
        "Scanner: enabled",
        f"Scanner worker: {worker_display}",
        f"State: {label}",
        f"Discovery: {discovery_label}",
        f"Baseline: {baseline}",
        f"Accumulated markets: {markets_accumulated}",
        f"Tracked markets: {tracked_markets}",
        f"Baseline-ready markets: {baseline_ready}",
        f"Qualifying opportunities: {active_count}",
    ]
    if progress is not None:
        lines.append(f"Discovery pages completed: {progress.pages_completed}")
    if last is not None:
        lines.append(
            "Last complete cycle (UTC): "
            f"{last.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
        )
    else:
        lines.append("Last complete cycle: none yet")
    if age is not None:
        lines.append(f"Cycle data age: {age}s")
    if progress_age is not None:
        lines.append(f"Discovery progress age: {progress_age}s")
    if viewer_live_mode is not None:
        lines.append(f"Live Mode: {live_mode_label(viewer_live_mode)}")
    lines.extend(
        [
            f"Automatic alerts: {'on' if auto_alerts else 'off'}",
            (
                "Automatic delivery to regular users: "
                f"{'available' if user_gate else 'disabled'}"
            ),
            (
                "Administrator delivery: "
                f"{'available' if admin_delivery_available else 'unavailable'}"
            ),
            "",
            "Live Mode and Scanner Status use the same arming state.",
            "This view does not place trades or refresh market data.",
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
        scanner_enabled=True,
        discovery_label=discovery_label,
        discovery_complete=discovery_complete,
        markets_accumulated=markets_accumulated,
        tracked_market_count=tracked_markets,
        baseline_ready_count=baseline_ready,
        progress_age_seconds=progress_age,
        auto_alerts_enabled=auto_alerts,
        admin_delivery_available=admin_delivery_available,
        admin_configuration_present=admin_configuration_present,
    )
