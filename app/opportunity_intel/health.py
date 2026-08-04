"""Sanitized Market Opportunity Scanner health evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.config.settings import Settings
from app.database import opportunity_storage
from app.opportunity_intel.models import OpportunityDiagnosticCode, ScannerStatus
from app.opportunity_intel.runtime import RUNTIME_BOT_DATA_KEY
from app.opportunity_intel.discovery import ResultStatus

OpportunityHealthState = Literal[
    "DISABLED",
    "NOT_INITIALIZED",
    "BASELINING",
    "HEALTHY",
    "DEGRADED",
    "STALE",
    "DOWN",
]

# Maps opportunity-specific states onto the global health monitor statuses.
_COMPONENT_STATUS_MAP: dict[OpportunityHealthState, str] = {
    "DISABLED": "DISABLED",
    "NOT_INITIALIZED": "HEALTHY",
    "BASELINING": "HEALTHY",
    "HEALTHY": "HEALTHY",
    "DEGRADED": "DEGRADED",
    "STALE": "DEGRADED",
    "DOWN": "DOWN",
}

DEGRADED_FAILURE_RATIO = 0.35
STALE_CYCLE_MULTIPLIER = 3


@dataclass(frozen=True)
class OpportunityHealthSnapshot:
    state: OpportunityHealthState
    detail: str
    metrics: dict[str, Any]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ratio(failed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return float(failed) / float(total)


def evaluate_opportunity_health(
    settings: Settings,
    *,
    bot_data: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> OpportunityHealthSnapshot:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not settings.opportunity_scanner_enabled:
        return OpportunityHealthSnapshot(
            state="DISABLED",
            detail="Opportunity scanner disabled",
            metrics={"opportunity_state": "DISABLED"},
        )

    try:
        opportunity_storage.initialize_opportunity_schema(settings)
        scanner_state = opportunity_storage.load_scanner_state(settings)
        runtime = opportunity_storage.load_runtime_snapshot(settings) or {}
        if bot_data and isinstance(bot_data.get(RUNTIME_BOT_DATA_KEY), dict):
            runtime = {**runtime, **bot_data[RUNTIME_BOT_DATA_KEY]}
        delivery_counts = opportunity_storage.count_deliveries_by_status(settings)
    except Exception:
        return OpportunityHealthSnapshot(
            state="DOWN",
            detail="Opportunity storage unavailable",
            metrics={
                "opportunity_state": "DOWN",
                "failure_categories": {"storage": 1},
            },
        )

    last_complete = None
    if scanner_state is not None and scanner_state.last_complete_cycle_at is not None:
        last_complete = scanner_state.last_complete_cycle_at.astimezone(timezone.utc)
    runtime_complete = _parse_iso(runtime.get("last_complete_cycle_at"))
    if runtime_complete is not None and (
        last_complete is None or runtime_complete > last_complete
    ):
        last_complete = runtime_complete

    last_attempt = _parse_iso(runtime.get("last_attempt_at"))
    cycle_age = (
        int((clock - last_complete).total_seconds()) if last_complete is not None else None
    )
    stale_after = int(settings.opportunity_scan_interval_seconds) * STALE_CYCLE_MULTIPLIER
    skipped_overlaps = int(runtime.get("skipped_overlaps") or 0)
    failure_categories = dict(runtime.get("failure_categories") or {})
    books_valid = int(runtime.get("books_valid") or 0)
    books_failed = int(runtime.get("books_failed") or 0)
    histories_valid = int(runtime.get("histories_valid") or 0)
    histories_failed = int(runtime.get("histories_failed") or 0)
    book_ratio = _ratio(books_failed, books_valid + books_failed)
    history_ratio = _ratio(histories_failed, histories_valid + histories_failed)

    pending = int(delivery_counts.get("pending") or 0)
    retry_wait = int(
        delivery_counts.get("retry_wait") or delivery_counts.get("failed") or 0
    )
    dead = int(delivery_counts.get("dead_letter") or 0)

    metrics: dict[str, Any] = {
        "opportunity_state": "NOT_INITIALIZED",
        "last_complete_cycle_at": last_complete.isoformat() if last_complete else None,
        "last_attempt_at": last_attempt.isoformat() if last_attempt else None,
        "cycle_age_seconds": cycle_age,
        "baseline_cycles": (
            int(scanner_state.global_baseline_cycles) if scanner_state else 0
        ),
        "markets_discovered": int(runtime.get("markets_discovered") or 0),
        "outcomes_shortlisted": int(runtime.get("outcomes_shortlisted") or 0),
        "observations_eligible": int(runtime.get("observations_eligible") or 0),
        "book_failure_ratio": round(book_ratio, 4),
        "history_failure_ratio": round(history_ratio, 4),
        "skipped_overlaps": skipped_overlaps,
        "delivery_pending": pending,
        "delivery_retry": retry_wait,
        "delivery_dead_letter": dead,
        "failure_categories": {
            str(k)[:64]: int(v)
            for k, v in failure_categories.items()
            if int(v) > 0
        },
    }

    if scanner_state is None and last_complete is None:
        return OpportunityHealthSnapshot(
            state="NOT_INITIALIZED",
            detail="No successful opportunity cycle yet",
            metrics={**metrics, "opportunity_state": "NOT_INITIALIZED"},
        )

    if scanner_state is not None and scanner_state.status == ScannerStatus.DOWN:
        return OpportunityHealthSnapshot(
            state="DOWN",
            detail="Opportunity scanner reported down",
            metrics={**metrics, "opportunity_state": "DOWN"},
        )

    discovery_status = str(runtime.get("discovery_status") or "")
    storage_failed = runtime.get("storage_ok") is False
    discovery_in_progress = (
        discovery_status == ResultStatus.IN_PROGRESS.value
        or int((failure_categories or {}).get("discovery_in_progress") or 0) > 0
        or OpportunityDiagnosticCode.DISCOVERY_IN_PROGRESS.value
        in list(runtime.get("diagnostic_codes") or [])
    )
    discovery_incomplete = (
        discovery_status
        in {
            ResultStatus.PERMANENT_FAILURE.value,
            ResultStatus.RETRYABLE_FAILURE.value,
            ResultStatus.EXPIRED.value,
        }
        or int((failure_categories or {}).get("discovery_incomplete") or 0) > 0
    )
    if storage_failed:
        return OpportunityHealthSnapshot(
            state="DOWN",
            detail="Opportunity discovery/storage unavailable",
            metrics={**metrics, "opportunity_state": "DOWN"},
        )
    if discovery_in_progress and last_complete is None:
        return OpportunityHealthSnapshot(
            state="BASELINING",
            detail="Opportunity discovery in progress across bounded cycles",
            metrics={
                **metrics,
                "opportunity_state": "BASELINING",
                "discovery_session_state": ResultStatus.IN_PROGRESS.value,
            },
        )
    if (
        last_attempt is not None
        and last_complete is None
        and discovery_incomplete
    ):
        return OpportunityHealthSnapshot(
            state="DOWN",
            detail="Opportunity discovery/storage unavailable",
            metrics={**metrics, "opportunity_state": "DOWN"},
        )
    if (
        last_attempt is not None
        and last_complete is None
        and int(runtime.get("markets_discovered") or 0) == 0
        and discovery_status
        and discovery_status
        not in {
            ResultStatus.COMPLETE.value,
            ResultStatus.IN_PROGRESS.value,
            ResultStatus.NOT_STARTED.value,
            "",
        }
    ):
        return OpportunityHealthSnapshot(
            state="DOWN",
            detail="Opportunity discovery/storage unavailable",
            metrics={**metrics, "opportunity_state": "DOWN"},
        )

    if scanner_state is not None and (
        scanner_state.status == ScannerStatus.BASELINING
        or scanner_state.global_baseline_cycles < 2
    ):
        return OpportunityHealthSnapshot(
            state="BASELINING",
            detail="Opportunity baseline incomplete",
            metrics={**metrics, "opportunity_state": "BASELINING"},
        )

    if cycle_age is not None and cycle_age > stale_after:
        return OpportunityHealthSnapshot(
            state="STALE",
            detail="Last complete opportunity cycle is stale",
            metrics={**metrics, "opportunity_state": "STALE"},
        )

    delivery_degraded = dead > 0 or retry_wait >= 5 or pending >= 20
    asset_degraded = (
        book_ratio >= DEGRADED_FAILURE_RATIO
        or history_ratio >= DEGRADED_FAILURE_RATIO
        or int(failure_categories.get("book_budget_skipped") or 0) > 0
    )
    if asset_degraded or delivery_degraded:
        return OpportunityHealthSnapshot(
            state="DEGRADED",
            detail=(
                "Opportunity delivery degraded"
                if delivery_degraded and not asset_degraded
                else "Opportunity cycle partially degraded"
            ),
            metrics={**metrics, "opportunity_state": "DEGRADED"},
        )

    if last_complete is None:
        return OpportunityHealthSnapshot(
            state="NOT_INITIALIZED",
            detail="No successful opportunity cycle yet",
            metrics={**metrics, "opportunity_state": "NOT_INITIALIZED"},
        )

    return OpportunityHealthSnapshot(
        state="HEALTHY",
        detail="Opportunity scanner healthy",
        metrics={**metrics, "opportunity_state": "HEALTHY"},
    )


def component_status_for_opportunity(state: OpportunityHealthState) -> str:
    return _COMPONENT_STATUS_MAP[state]


def evaluate_component_states(
    settings: Settings,
    *,
    bot_data: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, OpportunityHealthSnapshot]:
    """Per-component views sharing one sanitized aggregate evaluation."""
    overall = evaluate_opportunity_health(settings, bot_data=bot_data, now=now)
    metrics = overall.metrics
    state = overall.state

    def _snap(component_state: OpportunityHealthState, detail: str) -> OpportunityHealthSnapshot:
        return OpportunityHealthSnapshot(
            state=component_state,
            detail=detail,
            metrics={**metrics, "opportunity_state": component_state},
        )

    if state == "DISABLED":
        disabled = _snap("DISABLED", "Opportunity scanner disabled")
        return {
            "opportunity_discovery": disabled,
            "opportunity_history": disabled,
            "opportunity_books": disabled,
            "opportunity_scheduler": disabled,
            "opportunity_storage": disabled,
            "opportunity_delivery": disabled,
            "opportunity_baseline": disabled,
        }

    discovery_state: OpportunityHealthState = state
    if metrics.get("markets_discovered", 0) == 0 and state not in {
        "NOT_INITIALIZED",
        "BASELINING",
    }:
        discovery_state = "DOWN" if state == "DOWN" else "DEGRADED"

    history_ratio = float(metrics.get("history_failure_ratio") or 0.0)
    book_ratio = float(metrics.get("book_failure_ratio") or 0.0)
    history_state: OpportunityHealthState = (
        "DEGRADED" if history_ratio >= DEGRADED_FAILURE_RATIO else state
    )
    books_state: OpportunityHealthState = (
        "DEGRADED" if book_ratio >= DEGRADED_FAILURE_RATIO else state
    )
    if state == "DOWN":
        history_state = "DOWN"
        books_state = "DOWN"

    delivery_state: OpportunityHealthState = state
    if int(metrics.get("delivery_dead_letter") or 0) > 0 or int(
        metrics.get("delivery_retry") or 0
    ) >= 5:
        delivery_state = "DEGRADED" if state != "DOWN" else "DOWN"
    if state in {"HEALTHY", "BASELINING", "NOT_INITIALIZED"} and delivery_state == "DEGRADED":
        # Delivery may degrade independently.
        pass

    baseline_state: OpportunityHealthState = state
    if state == "BASELINING" or int(metrics.get("baseline_cycles") or 0) < 2:
        baseline_state = "BASELINING" if state != "DISABLED" else "DISABLED"

    storage_state: OpportunityHealthState = (
        "DOWN" if state == "DOWN" and "storage" in str(overall.detail).lower() else state
    )
    if "storage" in (metrics.get("failure_categories") or {}):
        storage_state = "DOWN"

    scheduler_state = state
    if int(metrics.get("skipped_overlaps") or 0) > 10 and state == "HEALTHY":
        scheduler_state = "DEGRADED"

    return {
        "opportunity_discovery": _snap(discovery_state, f"Discovery {discovery_state}"),
        "opportunity_history": _snap(history_state, f"History {history_state}"),
        "opportunity_books": _snap(books_state, f"Books {books_state}"),
        "opportunity_scheduler": _snap(scheduler_state, f"Scheduler {scheduler_state}"),
        "opportunity_storage": _snap(storage_state, f"Storage {storage_state}"),
        "opportunity_delivery": _snap(delivery_state, f"Delivery {delivery_state}"),
        "opportunity_baseline": _snap(baseline_state, f"Baseline {baseline_state}"),
    }
