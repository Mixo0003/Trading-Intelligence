"""Opportunity scanner runtime wrapper (no Telegram send ownership)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config.settings import Settings
from app.database import opportunity_storage
from app.opportunity_intel.deadline import CycleDeadlineToken
from app.opportunity_intel.delivery import select_delivery_wave
from app.opportunity_intel.delivery_gates import OpportunityDeliveryGates
from app.opportunity_intel.discovery import (
    CompleteDiscoveryCache,
    DiscoveryResult,
    ResultStatus,
)
from app.opportunity_intel.models import LiveMode, OpportunityDiagnosticCode, ScannerStatus
from app.opportunity_intel.request_budget import CycleRequestBudget
from app.opportunity_intel.scanner import OpportunityScannerService, ScannerCycleResult
from app.opportunity_intel.scanner_control import scanner_armed
from app.opportunity_intel.versions import (
    LOCAL_CLOB_REQUEST_CAP_PER_CYCLE,
    LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
    SCANNER_HTTP_CONCURRENCY,
)
from app.polymarket.client import PolymarketClient

logger = logging.getLogger(__name__)

RUNTIME_BOT_DATA_KEY = "opportunity_runtime"

# Process-local dedicated Opportunity executor. Bounded, created once, never
# shared with Copy/default asyncio executor work.
_EXECUTOR_LOCK = threading.Lock()
_CYCLE_EXECUTOR: ThreadPoolExecutor | None = None
_OPPORTUNITY_EXECUTOR_MAX_WORKERS = 2
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_FUTURES: set[Future] = set()

# Process-local discovery cache. A completed universe must outlive the cycle
# that published it, otherwise every cycle restarts a full keyset crawl and no
# two consecutive cycles ever see a trusted catalog.
_DISCOVERY_CACHE_LOCK = threading.Lock()
_DISCOVERY_CACHE: CompleteDiscoveryCache | None = None
_DISCOVERY_CACHE_KEY: tuple[str, int, int, int] | None = None


def _discovery_cache_key(settings: Settings) -> tuple[str, int, int, int]:
    return (
        str(settings.database_path),
        int(settings.opportunity_discovery_ttl_seconds),
        int(settings.opportunity_catalog_ttl_seconds),
        int(settings.opportunity_discovery_session_ttl_seconds),
    )


def get_discovery_cache(settings: Settings) -> CompleteDiscoveryCache:
    """Idempotent per-process discovery cache reused across scanner cycles."""
    global _DISCOVERY_CACHE, _DISCOVERY_CACHE_KEY
    key = _discovery_cache_key(settings)
    with _DISCOVERY_CACHE_LOCK:
        if _DISCOVERY_CACHE is None or _DISCOVERY_CACHE_KEY != key:
            _DISCOVERY_CACHE = CompleteDiscoveryCache(
                ttl_seconds=settings.opportunity_discovery_ttl_seconds,
                catalog_ttl_seconds=settings.opportunity_catalog_ttl_seconds,
                session_ttl_seconds=settings.opportunity_discovery_session_ttl_seconds,
                session_store=opportunity_storage.SqliteDiscoverySessionStore(settings),
                settings=settings,
                persist_catalog=True,
            )
            _DISCOVERY_CACHE_KEY = key
        return _DISCOVERY_CACHE


def reset_discovery_cache() -> None:
    """Drop the process-local discovery cache (tests and shutdown helpers)."""
    global _DISCOVERY_CACHE, _DISCOVERY_CACHE_KEY
    with _DISCOVERY_CACHE_LOCK:
        _DISCOVERY_CACHE = None
        _DISCOVERY_CACHE_KEY = None


def get_opportunity_executor() -> ThreadPoolExecutor:
    """Idempotent dedicated executor for Opportunity scanner cycles only."""
    global _CYCLE_EXECUTOR
    with _EXECUTOR_LOCK:
        if _CYCLE_EXECUTOR is None:
            _CYCLE_EXECUTOR = ThreadPoolExecutor(
                max_workers=_OPPORTUNITY_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="opp-cycle",
            )
        return _CYCLE_EXECUTOR


def shutdown_opportunity_executor(*, wait: bool = False) -> None:
    """Test/helper shutdown. Production shutdown expires tokens instead."""
    global _CYCLE_EXECUTOR
    with _EXECUTOR_LOCK:
        executor = _CYCLE_EXECUTOR
        _CYCLE_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=False)


@dataclass(frozen=True)
class CycleRuntimeResult:
    enabled: bool
    skipped_overlap: bool = False
    timed_out: bool = False
    selection_ran: bool = False
    waves_selected: int = 0
    pending_inserted: int = 0
    cycle: ScannerCycleResult | None = None
    diagnostic_codes: tuple[OpportunityDiagnosticCode, ...] = ()
    failure_categories: dict[str, int] = field(default_factory=dict)
    executor_future_pending: bool = False
    late_completion_suppressed: bool = False


@dataclass
class ManagedCycleHandle:
    """Tracks one in-flight scanner future after the await deadline may expire."""

    future: Future
    deadline_token: CycleDeadlineToken
    request_budget: CycleRequestBudget
    settings: Settings
    clock: datetime
    bot_data: dict[str, Any] | None
    timed_out: bool = False


def sync_user_delivery_gate_from_settings(settings: Settings) -> None:
    """Keep DB user gate aligned with immutable process config."""
    opportunity_storage.initialize_opportunity_schema(settings)
    current = opportunity_storage.load_delivery_gates(settings)
    desired = bool(settings.opportunity_user_delivery_enabled)
    if current.user_delivery_enabled == desired:
        return
    opportunity_storage.save_delivery_gates(
        settings,
        OpportunityDeliveryGates(
            admin_delivery_enabled=current.admin_delivery_enabled,
            user_delivery_enabled=desired,
        ),
    )


def any_strict_live_demand(settings: Settings) -> bool:
    for subscription in opportunity_storage.list_subscriptions(settings):
        if (
            subscription.enabled
            and subscription.settings.live_mode == LiveMode.STRICT
        ):
            return True
    return False


def build_scanner_service(
    settings: Settings,
    *,
    discovery_cache: CompleteDiscoveryCache | None = None,
    gamma_client=None,
    history_client=None,
    book_client=None,
    request_budget: CycleRequestBudget | None = None,
    deadline_token: CycleDeadlineToken | None = None,
) -> OpportunityScannerService:
    cache = discovery_cache or get_discovery_cache(settings)
    budget = request_budget or CycleRequestBudget(
        clob_cap=LOCAL_CLOB_REQUEST_CAP_PER_CYCLE,
        gamma_cap=LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
    )
    return OpportunityScannerService(
        settings,
        discovery_cache=cache,
        history_client=history_client,
        book_client=book_client,
        concurrency=min(SCANNER_HTTP_CONCURRENCY, settings.opportunity_concurrency),
        gamma_client=gamma_client or PolymarketClient(settings),
        max_markets=settings.opportunity_max_markets,
        max_books_per_cycle=settings.opportunity_max_books_per_cycle,
        max_live_books_per_cycle=settings.opportunity_max_live_books_per_cycle,
        gamma_max_pages=LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
        request_budget=budget,
        deadline_token=deadline_token,
    )


def _sanitize_failure_categories(raw: dict[str, int] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in (raw or {}).items():
        label = str(key)[:64]
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        out[label] = count
    return out


def persist_runtime_snapshot(
    settings: Settings,
    *,
    now: datetime,
    result: CycleRuntimeResult,
    skipped_overlaps: int,
    bot_data: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    cycle = result.cycle
    payload = {
        "enabled": bool(scanner_armed(settings)),
        "last_attempt_at": now.astimezone(timezone.utc).isoformat(),
        "skipped_overlaps": int(skipped_overlaps),
        "timed_out": bool(result.timed_out),
        "selection_ran": bool(result.selection_ran),
        "waves_selected": int(result.waves_selected),
        "pending_inserted": int(result.pending_inserted),
        "failure_categories": _sanitize_failure_categories(result.failure_categories),
        "diagnostic_codes": [code.value for code in result.diagnostic_codes][:20],
        "executor_future_pending": bool(result.executor_future_pending),
        "late_completion_suppressed": bool(result.late_completion_suppressed),
    }
    if cycle is not None:
        payload.update(
            {
                "cycle_id": cycle.cycle_id,
                "discovery_status": cycle.discovery_status.value,
                "markets_discovered": cycle.markets_discovered,
                "outcomes_shortlisted": cycle.outcomes_shortlisted,
                "histories_valid": cycle.histories_valid,
                "histories_failed": cycle.histories_failed,
                "books_valid": cycle.books_valid,
                "books_failed": cycle.books_failed,
                "observations_eligible": cycle.observations_eligible,
                "observations_rejected": cycle.observations_rejected,
                "baseline_advanced": cycle.baseline_advanced,
                "signals_created": cycle.signals_created,
                "storage_ok": cycle.storage_ok,
                "clob_attempts_used": cycle.clob_attempts_used,
                "gamma_attempts_used": cycle.gamma_attempts_used,
                "budget_exhausted_assets": cycle.budget_exhausted_assets,
                "deadline_aborted_before_commit": cycle.deadline_aborted_before_commit,
                "discovery_session_state": (
                    cycle.discovery_status.value if cycle.discovery_status else None
                ),
                "failure_categories": _sanitize_failure_categories(
                    {**cycle.failure_counters, **result.failure_categories}
                ),
            }
        )
        if (
            cycle.storage_ok
            and cycle.discovery_status == ResultStatus.COMPLETE
            and not result.timed_out
            and not cycle.deadline_aborted_before_commit
        ):
            payload["last_complete_cycle_at"] = now.astimezone(timezone.utc).isoformat()
    if extra:
        for key, value in extra.items():
            if key in {
                "path",
                "secret",
                "token",
                "url",
                "asset_id",
                "payload",
            }:
                continue
            payload[str(key)[:64]] = value
    opportunity_storage.save_runtime_snapshot(settings, payload)
    if bot_data is not None:
        bot_data[RUNTIME_BOT_DATA_KEY] = dict(payload)


def _track_future(future: Future) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT_FUTURES.add(future)

    def _done(done: Future) -> None:
        with _INFLIGHT_LOCK:
            _INFLIGHT_FUTURES.discard(done)

    future.add_done_callback(_done)


def has_inflight_cycle() -> bool:
    with _INFLIGHT_LOCK:
        return any(not fut.done() for fut in _INFLIGHT_FUTURES)


def inflight_cycle_count() -> int:
    with _INFLIGHT_LOCK:
        return sum(1 for fut in _INFLIGHT_FUTURES if not fut.done())


def _finalize_successful_cycle(
    settings: Settings,
    *,
    cycle: ScannerCycleResult,
    clock: datetime,
    deadline_token: CycleDeadlineToken | None,
    bot_data: dict[str, Any] | None,
    suppress_selection: bool,
) -> CycleRuntimeResult:
    selection_ran = False
    waves_selected = 0
    pending_inserted = 0
    diagnostics = tuple(cycle.diagnostic_codes)
    failure_categories = _sanitize_failure_categories(cycle.failure_counters)
    timed_out = bool(
        suppress_selection
        or (deadline_token is not None and deadline_token.expired)
        or cycle.deadline_aborted_before_commit
    )
    can_select = (
        not timed_out
        and cycle.storage_ok
        and cycle.discovery_status == ResultStatus.COMPLETE
        and OpportunityDiagnosticCode.DISCOVERY_INCOMPLETE not in diagnostics
        and OpportunityDiagnosticCode.DISCOVERY_IN_PROGRESS not in diagnostics
        and OpportunityDiagnosticCode.STORAGE_FAILURE not in diagnostics
        and OpportunityDiagnosticCode.CYCLE_DEADLINE_EXCEEDED not in diagnostics
    )
    if can_select and cycle.delivery_signal_uids:
        selection_ran = True
        for uid in cycle.delivery_signal_uids:
            try:
                wave = select_delivery_wave(
                    settings,
                    opportunity_uid=uid,
                    wave_at=clock,
                    now=clock,
                )
            except Exception:
                logger.exception("Delivery wave selection failed for signal")
                failure_categories["wave_selection"] = (
                    failure_categories.get("wave_selection", 0) + 1
                )
                continue
            waves_selected += 1
            pending_inserted += len(wave.pending)

    snapshot = opportunity_storage.load_runtime_snapshot(settings) or {}
    skipped = int(snapshot.get("skipped_overlaps") or 0)
    result = CycleRuntimeResult(
        enabled=True,
        timed_out=timed_out,
        selection_ran=selection_ran,
        waves_selected=waves_selected,
        pending_inserted=pending_inserted,
        cycle=cycle,
        diagnostic_codes=diagnostics
        if not timed_out
        else diagnostics + (OpportunityDiagnosticCode.CYCLE_DEADLINE_EXCEEDED,),
        failure_categories=failure_categories
        if not timed_out
        else {**failure_categories, "cycle_deadline": 1},
        late_completion_suppressed=bool(suppress_selection and timed_out),
    )
    persist_runtime_snapshot(
        settings,
        now=clock,
        result=result,
        skipped_overlaps=skipped,
        bot_data=bot_data,
    )
    return result


def _execute_cycle(
    settings: Settings,
    *,
    clock: datetime,
    service: OpportunityScannerService | None,
    discovery_result: DiscoveryResult | None,
    request_budget: CycleRequestBudget,
    deadline_token: CycleDeadlineToken | None,
) -> ScannerCycleResult:
    scanner = service or build_scanner_service(
        settings,
        request_budget=request_budget,
        deadline_token=deadline_token,
    )
    if service is not None:
        scanner.deadline_token = deadline_token
        scanner.request_budget = request_budget
        if getattr(scanner.history_client, "request_budget", None) is None:
            scanner.history_client.request_budget = request_budget
        if getattr(scanner.book_client, "request_budget", None) is None:
            scanner.book_client.request_budget = request_budget
    strict_live = any_strict_live_demand(settings)
    return scanner.run_cycle(
        now=clock,
        strict_live_demand=strict_live,
        discovery_result=discovery_result,
    )


def start_managed_cycle(
    settings: Settings,
    *,
    now: datetime | None = None,
    service: OpportunityScannerService | None = None,
    discovery_result: DiscoveryResult | None = None,
    bot_data: dict[str, Any] | None = None,
    deadline_seconds: float | None = None,
) -> ManagedCycleHandle | CycleRuntimeResult:
    """Submit one cycle to the shared executor. Caller awaits with containment."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not scanner_armed(settings):
        return CycleRuntimeResult(enabled=False)

    opportunity_storage.initialize_opportunity_schema(settings)
    sync_user_delivery_gate_from_settings(settings)

    timeout = (
        float(settings.opportunity_cycle_deadline_seconds)
        if deadline_seconds is None
        else float(deadline_seconds)
    )
    token = CycleDeadlineToken.from_timeout_seconds(timeout)
    budget = CycleRequestBudget(
        clob_cap=LOCAL_CLOB_REQUEST_CAP_PER_CYCLE,
        gamma_cap=LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
    )
    future = get_opportunity_executor().submit(
        _execute_cycle,
        settings,
        clock=clock,
        service=service,
        discovery_result=discovery_result,
        request_budget=budget,
        deadline_token=token,
    )
    _track_future(future)
    return ManagedCycleHandle(
        future=future,
        deadline_token=token,
        request_budget=budget,
        settings=settings,
        clock=clock,
        bot_data=bot_data,
    )


def await_managed_cycle(
    handle: ManagedCycleHandle,
    *,
    timeout_seconds: float | None = None,
) -> CycleRuntimeResult:
    """Await cycle up to timeout without shutting down the executor worker.

    Design: cooperative deadline token + deferred overlap release.
    On timeout the future remains tracked; selection/sends are suppressed
    even if a late atomic commit completes.
    """
    timeout = (
        float(handle.settings.opportunity_cycle_deadline_seconds)
        if timeout_seconds is None
        else float(timeout_seconds)
    )
    try:
        cycle = handle.future.result(timeout=timeout)
    except FuturesTimeoutError:
        handle.timed_out = True
        handle.deadline_token.expire()
        snapshot = opportunity_storage.load_runtime_snapshot(handle.settings) or {}
        skipped = int(snapshot.get("skipped_overlaps") or 0)
        metrics = handle.request_budget.sanitized_metrics()
        result = CycleRuntimeResult(
            enabled=True,
            timed_out=True,
            diagnostic_codes=(OpportunityDiagnosticCode.CYCLE_DEADLINE_EXCEEDED,),
            failure_categories={"cycle_deadline": 1},
            executor_future_pending=not handle.future.done(),
        )
        persist_runtime_snapshot(
            handle.settings,
            now=handle.clock,
            result=result,
            skipped_overlaps=skipped,
            bot_data=handle.bot_data,
            extra=metrics,
        )
        return result
    except Exception:
        logger.exception("Opportunity scanner cycle failed")
        snapshot = opportunity_storage.load_runtime_snapshot(handle.settings) or {}
        skipped = int(snapshot.get("skipped_overlaps") or 0)
        from app.opportunity_intel.models import OpportunityScannerState
        from app.opportunity_intel.versions import (
            BASELINE_VERSION,
            OPPORTUNITY_SCORE_VERSION,
            OPPORTUNITY_SCHEMA_VERSION,
        )

        prior = opportunity_storage.load_scanner_state(handle.settings)
        try:
            opportunity_storage.save_scanner_state(
                handle.settings,
                OpportunityScannerState(
                    status=ScannerStatus.DOWN,
                    schema_version=(
                        prior.schema_version
                        if prior is not None
                        else OPPORTUNITY_SCHEMA_VERSION
                    ),
                    baseline_version=(
                        prior.baseline_version if prior is not None else BASELINE_VERSION
                    ),
                    score_version=(
                        prior.score_version
                        if prior is not None
                        else OPPORTUNITY_SCORE_VERSION
                    ),
                    global_baseline_cycles=(
                        prior.global_baseline_cycles if prior is not None else 0
                    ),
                    discovery_complete=False,
                    last_complete_discovery_at=(
                        prior.last_complete_discovery_at if prior is not None else None
                    ),
                    last_complete_cycle_at=(
                        prior.last_complete_cycle_at if prior is not None else None
                    ),
                    diagnostic_code=OpportunityDiagnosticCode.STORAGE_FAILURE,
                    updated_at=handle.clock,
                ),
            )
        except Exception:
            logger.debug("Failed to persist scanner DOWN state", exc_info=True)
        result_out = CycleRuntimeResult(
            enabled=True,
            diagnostic_codes=(OpportunityDiagnosticCode.STORAGE_FAILURE,),
            failure_categories={"cycle_exception": 1},
        )
        persist_runtime_snapshot(
            handle.settings,
            now=handle.clock,
            result=result_out,
            skipped_overlaps=skipped,
            bot_data=handle.bot_data,
        )
        return result_out

    return _finalize_successful_cycle(
        handle.settings,
        cycle=cycle,
        clock=handle.clock,
        deadline_token=handle.deadline_token,
        bot_data=handle.bot_data,
        suppress_selection=handle.timed_out,
    )


def finish_late_managed_cycle(handle: ManagedCycleHandle) -> CycleRuntimeResult | None:
    """Drain a timed-out future without delivery selection/sends."""
    if not handle.future.done():
        return None
    if handle.future.cancelled():
        return None
    try:
        cycle = handle.future.result(timeout=0)
    except Exception:
        logger.exception("Late opportunity cycle failed after timeout")
        return None
    handle.deadline_token.expire()
    result = _finalize_successful_cycle(
        handle.settings,
        cycle=cycle,
        clock=handle.clock,
        deadline_token=handle.deadline_token,
        bot_data=handle.bot_data,
        suppress_selection=True,
    )
    snapshot = opportunity_storage.load_runtime_snapshot(handle.settings) or {}
    skipped = int(snapshot.get("skipped_overlaps") or 0)
    persist_runtime_snapshot(
        handle.settings,
        now=datetime.now(timezone.utc),
        result=result,
        skipped_overlaps=skipped,
        bot_data=handle.bot_data,
        extra={
            **handle.request_budget.sanitized_metrics(),
            "late_worker_completed": 1,
            "late_commit_occurred": int(bool(cycle.storage_ok)),
        },
    )
    return result


def run_opportunity_scan_cycle(
    settings: Settings,
    *,
    now: datetime | None = None,
    service: OpportunityScannerService | None = None,
    discovery_result: DiscoveryResult | None = None,
    bot_data: dict[str, Any] | None = None,
    skip_deadline: bool = False,
) -> CycleRuntimeResult:
    """Run one bounded scanner cycle outside the Telegram event loop.

    When ``skip_deadline`` is False, uses managed containment: timeout returns
    immediately while the executor future remains tracked.
    """
    if not scanner_armed(settings):
        return CycleRuntimeResult(enabled=False)
    if skip_deadline:
        clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        opportunity_storage.initialize_opportunity_schema(settings)
        sync_user_delivery_gate_from_settings(settings)
        budget = CycleRequestBudget(
            clob_cap=LOCAL_CLOB_REQUEST_CAP_PER_CYCLE,
            gamma_cap=LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
        )
        cycle = _execute_cycle(
            settings,
            clock=clock,
            service=service,
            discovery_result=discovery_result,
            request_budget=budget,
            deadline_token=None,
        )
        return _finalize_successful_cycle(
            settings,
            cycle=cycle,
            clock=clock,
            deadline_token=None,
            bot_data=bot_data,
            suppress_selection=False,
        )

    started = start_managed_cycle(
        settings,
        now=now,
        service=service,
        discovery_result=discovery_result,
        bot_data=bot_data,
    )
    if isinstance(started, CycleRuntimeResult):
        return started
    result = await_managed_cycle(started)
    if result.timed_out and result.executor_future_pending:
        # Caller (scheduler) must keep overlap protection until future completes.
        started.timed_out = True
    return result


def record_skipped_overlap(
    settings: Settings,
    *,
    bot_data: dict[str, Any] | None = None,
) -> int:
    snapshot = opportunity_storage.load_runtime_snapshot(settings) or {}
    skipped = int(snapshot.get("skipped_overlaps") or 0) + 1
    snapshot["skipped_overlaps"] = skipped
    snapshot["last_overlap_skip_at"] = datetime.now(timezone.utc).isoformat()
    opportunity_storage.save_runtime_snapshot(settings, snapshot)
    if bot_data is not None:
        bot_data[RUNTIME_BOT_DATA_KEY] = dict(snapshot)
    return skipped
