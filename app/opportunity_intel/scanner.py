"""Pure Market Opportunity Scanner cycle orchestration (Checkpoint 4)."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.config.settings import Settings
from app.copy_intel.order_book import ValidatedOrderBook
from app.database.opportunity_storage import (
    CycleAssetWrite,
    MarketStateRecord,
    SCHEMA_VERSION,
    commit_scanner_cycle,
    load_active_signals_for_asset,
    load_all_market_states,
    next_episode_sequence,
)
from app.opportunity_intel.baseline import advance_baseline
from app.opportunity_intel.book import (
    BookAdapterFailure,
    PublicOrderBookClient,
)
from app.opportunity_intel.discovery import (
    CompleteDiscoveryCache,
    DiscoveryResult,
    OutcomeCandidate,
    ResultStatus,
    shortlist_outcomes,
)
from app.opportunity_intel.episodes import (
    build_opportunity_key,
    decide_episode_actions,
    direction_from_movement,
    identity_fingerprint,
    strongest_absolute_move_pp,
)
from app.opportunity_intel.history import (
    FreshnessStatus,
    MovementResult,
    PublicPriceHistoryClient,
    select_movement,
)
from app.opportunity_intel.live_confirm import (
    DelayFn,
    LiveConfirmationFailure,
    LiveConfirmationSuccess,
    confirm_strict_live_books,
    noop_delay,
)
from app.opportunity_intel.deadline import CycleDeadlineToken
from app.opportunity_intel.models import (
    ObservationMode,
    OpportunityDiagnosticCode,
    OpportunityScannerState,
    ScannerStatus,
)
from app.opportunity_intel.observation import (
    ObservationAccepted,
    ObservationRejected,
    build_canonical_observation,
)
from app.opportunity_intel.request_budget import CycleRequestBudget, RequestBudgetExhausted
from app.opportunity_intel.versions import (
    BASELINE_VERSION,
    LOCAL_CLOB_REQUEST_CAP_PER_CYCLE,
    LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
    OPPORTUNITY_SCORE_VERSION,
    SAFETY_POLICY_VERSION,
    SCANNER_HTTP_CONCURRENCY,
    STRICT_LIVE_CONFIRM_DELAY_SECONDS,
)


@dataclass(frozen=True)
class AssetEvaluationResult:
    asset_id: str
    mode: ObservationMode
    eligible: bool
    baseline_advanced: bool
    signal_created: bool
    signal_material_updated: bool
    signal_expired: bool
    diagnostic_codes: tuple[OpportunityDiagnosticCode, ...] = ()


@dataclass(frozen=True)
class ScannerCycleResult:
    cycle_id: str
    discovery_status: ResultStatus
    markets_discovered: int
    outcomes_shortlisted: int
    histories_valid: int
    histories_failed: int
    books_valid: int
    books_failed: int
    observations_eligible: int
    observations_rejected: int
    baseline_advanced: int
    signals_created: int
    signals_materially_updated: int
    signals_expired: int
    storage_ok: bool
    asset_results: tuple[AssetEvaluationResult, ...] = ()
    diagnostic_codes: tuple[OpportunityDiagnosticCode, ...] = ()
    failure_counters: dict[str, int] = field(default_factory=dict)
    delivery_signal_uids: tuple[str, ...] = ()
    clob_attempts_used: int = 0
    gamma_attempts_used: int = 0
    budget_exhausted_assets: int = 0
    deadline_aborted_before_commit: bool = False


class _GammaBudgetClient:
    def __init__(self, inner, budget: CycleRequestBudget) -> None:
        self._inner = inner
        self._budget = budget

    def get_json(self, path: str, params: dict | None = None):
        if not self._budget.try_consume_gamma(1):
            raise RequestBudgetExhausted(kind="gamma")
        return self._inner.get_json(path, params)


class OpportunityScannerService:
    """Bounded typed scanner cycle. No Telegram delivery and no scheduler ownership."""

    def __init__(
        self,
        settings: Settings,
        *,
        discovery_cache: CompleteDiscoveryCache | None = None,
        history_client: PublicPriceHistoryClient | None = None,
        book_client: PublicOrderBookClient | None = None,
        delay: DelayFn = noop_delay,
        concurrency: int = SCANNER_HTTP_CONCURRENCY,
        live_confirm_delay_seconds: float = STRICT_LIVE_CONFIRM_DELAY_SECONDS,
        gamma_client=None,
        max_markets: int | None = None,
        max_books_per_cycle: int | None = None,
        max_live_books_per_cycle: int | None = None,
        gamma_max_pages: int | None = None,
        request_budget: CycleRequestBudget | None = None,
        deadline_token: CycleDeadlineToken | None = None,
    ) -> None:
        self.settings = settings
        self.discovery_cache = discovery_cache or CompleteDiscoveryCache()
        self.request_budget = request_budget or CycleRequestBudget(
            clob_cap=LOCAL_CLOB_REQUEST_CAP_PER_CYCLE,
            gamma_cap=LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
        )
        self.deadline_token = deadline_token
        if history_client is not None:
            self.history_client = history_client
            if getattr(self.history_client, "request_budget", None) is None:
                self.history_client.request_budget = self.request_budget
        else:
            self.history_client = PublicPriceHistoryClient(
                settings, request_budget=self.request_budget
            )
        if book_client is not None:
            self.book_client = book_client
            if getattr(self.book_client, "request_budget", None) is None:
                self.book_client.request_budget = self.request_budget
        else:
            self.book_client = PublicOrderBookClient(
                settings, request_budget=self.request_budget
            )
        self.delay = delay
        self.concurrency = max(1, min(5, int(concurrency)))
        self.live_confirm_delay_seconds = float(live_confirm_delay_seconds)
        raw_gamma = gamma_client
        self.gamma_client = (
            None
            if raw_gamma is None
            else _GammaBudgetClient(raw_gamma, self.request_budget)
        )
        self.max_markets = max(1, min(100, int(max_markets or 100)))
        self.max_books_per_cycle = max(1, min(40, int(max_books_per_cycle or 40)))
        self.max_live_books_per_cycle = max(
            1, min(10, int(max_live_books_per_cycle or 10))
        )
        self.gamma_max_pages = max(1, min(10, int(gamma_max_pages or 10)))

    def run_cycle(
        self,
        *,
        now: datetime,
        strict_live_demand: bool,
        discovery_result: DiscoveryResult | None = None,
    ) -> ScannerCycleResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = now.astimezone(timezone.utc)
        cycle_id = f"cycle-{current.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

        if discovery_result is not None:
            discovery = discovery_result
        else:
            if self.gamma_client is None:
                raise ValueError("gamma_client is required unless discovery_result is provided")
            discovery = self.discovery_cache.advance_from_client(
                self.gamma_client,
                now=current,
                max_attempts=self.gamma_max_pages,
            )
        budget_snap = self.request_budget.snapshot()
        gamma_used = max(
            int(discovery.gamma_attempts_this_cycle),
            int(budget_snap.gamma_attempts_used),
        )
        if discovery.status == ResultStatus.IN_PROGRESS:
            return ScannerCycleResult(
                cycle_id=cycle_id,
                discovery_status=discovery.status,
                markets_discovered=0,
                outcomes_shortlisted=0,
                histories_valid=0,
                histories_failed=0,
                books_valid=0,
                books_failed=0,
                observations_eligible=0,
                observations_rejected=0,
                baseline_advanced=0,
                signals_created=0,
                signals_materially_updated=0,
                signals_expired=0,
                storage_ok=True,
                diagnostic_codes=(OpportunityDiagnosticCode.DISCOVERY_IN_PROGRESS,),
                failure_counters={"discovery_in_progress": 1},
                clob_attempts_used=budget_snap.clob_attempts_used,
                gamma_attempts_used=gamma_used,
                budget_exhausted_assets=budget_snap.clob_exhausted_count,
            )
        if not discovery.complete:
            failure_code = OpportunityDiagnosticCode.DISCOVERY_INCOMPLETE
            if discovery.diagnostics:
                failure_code = discovery.diagnostics[0].code
            return ScannerCycleResult(
                cycle_id=cycle_id,
                discovery_status=discovery.status,
                markets_discovered=len(discovery.markets),
                outcomes_shortlisted=0,
                histories_valid=0,
                histories_failed=0,
                books_valid=0,
                books_failed=0,
                observations_eligible=0,
                observations_rejected=0,
                baseline_advanced=0,
                signals_created=0,
                signals_materially_updated=0,
                signals_expired=0,
                storage_ok=True,
                diagnostic_codes=(failure_code,),
                failure_counters={"discovery_incomplete": 1},
                clob_attempts_used=budget_snap.clob_attempts_used,
                gamma_attempts_used=gamma_used,
                budget_exhausted_assets=budget_snap.clob_exhausted_count,
            )

        shortlist = shortlist_outcomes(
            discovery.markets,
            now=current,
            strict_live_demand=strict_live_demand,
            max_candidates=self.max_markets,
        )
        shortlist, budget_skipped = self._apply_book_budgets(shortlist)
        if self.deadline_token is not None and self.deadline_token.expired:
            snap = self.request_budget.snapshot()
            return ScannerCycleResult(
                cycle_id=cycle_id,
                discovery_status=ResultStatus.COMPLETE,
                markets_discovered=len(discovery.markets),
                outcomes_shortlisted=len(shortlist),
                histories_valid=0,
                histories_failed=0,
                books_valid=0,
                books_failed=0,
                observations_eligible=0,
                observations_rejected=0,
                baseline_advanced=0,
                signals_created=0,
                signals_materially_updated=0,
                signals_expired=0,
                storage_ok=False,
                diagnostic_codes=(OpportunityDiagnosticCode.CYCLE_DEADLINE_EXCEEDED,),
                failure_counters={"cycle_deadline": 1},
                clob_attempts_used=snap.clob_attempts_used,
                gamma_attempts_used=snap.gamma_attempts_used,
                budget_exhausted_assets=snap.clob_exhausted_count,
                deadline_aborted_before_commit=True,
            )
        prior_states = {
            state.asset_id: state for state in load_all_market_states(self.settings)
        }
        present_assets = {candidate.asset_id for candidate in shortlist}

        # Phase 1: HTTP + pure evaluation (no SQLite write transaction held).
        evaluations: list[tuple[OutcomeCandidate, dict]] = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(self._evaluate_candidate, candidate, current): candidate
                for candidate in shortlist
            }
            for future in as_completed(futures):
                if self.deadline_token is not None and self.deadline_token.expired:
                    break
                candidate = futures[future]
                evaluations.append((candidate, future.result()))

        # Phase 2: build write set from evaluations + prior state.
        writes: list[CycleAssetWrite] = []
        asset_results: list[AssetEvaluationResult] = []
        counters = {
            "histories_valid": 0,
            "histories_failed": 0,
            "books_valid": 0,
            "books_failed": 0,
            "observations_eligible": 0,
            "observations_rejected": 0,
            "baseline_advanced": 0,
            "signals_created": 0,
            "signals_materially_updated": 0,
            "signals_expired": 0,
        }
        failure_counters: dict[str, int] = {}
        if budget_skipped:
            failure_counters["book_budget_skipped"] = budget_skipped

        for candidate, payload in evaluations:
            result, write = self._build_asset_write(
                candidate=candidate,
                payload=payload,
                prior=prior_states.get(candidate.asset_id),
                cycle_id=cycle_id,
                now=current,
                present_in_discovery=True,
            )
            asset_results.append(result)
            if write is not None:
                writes.append(write)
            if result.eligible:
                counters["observations_eligible"] += 1
            else:
                counters["observations_rejected"] += 1
            if result.baseline_advanced:
                counters["baseline_advanced"] += 1
            if result.signal_created:
                counters["signals_created"] += 1
            if result.signal_material_updated:
                counters["signals_materially_updated"] += 1
            if result.signal_expired:
                counters["signals_expired"] += 1
            if payload.get("history_ok"):
                counters["histories_valid"] += 1
            else:
                counters["histories_failed"] += 1
                failure_counters["history"] = failure_counters.get("history", 0) + 1
            if payload.get("book_ok"):
                counters["books_valid"] += 1
            else:
                counters["books_failed"] += 1
                failure_counters["book"] = failure_counters.get("book", 0) + 1

        # Absence resets for markets missing beyond window (no HTTP).
        for asset_id, prior in prior_states.items():
            if asset_id in present_assets:
                continue
            reset_write = self._absence_write(
                prior=prior, cycle_id=cycle_id, now=current
            )
            if reset_write is not None:
                writes.append(reset_write)

        min_global = 2
        for write in writes:
            min_global = min(min_global, write.baseline.consecutive_complete_cycles)
        if not writes:
            min_global = 0
        else:
            # Global baseline cycles tracks the minimum ready progress across written assets.
            min_global = min(write.baseline.consecutive_complete_cycles for write in writes)

        scanner_state = OpportunityScannerState(
            status=(
                ScannerStatus.BASELINING
                if min_global < 2
                else ScannerStatus.HEALTHY
            ),
            schema_version=SCHEMA_VERSION,
            baseline_version=BASELINE_VERSION,
            score_version=OPPORTUNITY_SCORE_VERSION,
            global_baseline_cycles=min(2, min_global),
            discovery_complete=True,
            last_complete_discovery_at=current,
            last_complete_cycle_at=current,
            diagnostic_code=None,
            updated_at=current,
        )

        budget_snap = self.request_budget.snapshot()
        if budget_snap.clob_exhausted_count:
            failure_counters["clob_budget_exhausted"] = budget_snap.clob_exhausted_count

        if self.deadline_token is not None and self.deadline_token.expired:
            # Cooperative stop before opening the SQLite write transaction.
            return ScannerCycleResult(
                cycle_id=cycle_id,
                discovery_status=ResultStatus.COMPLETE,
                markets_discovered=len(discovery.markets),
                outcomes_shortlisted=len(shortlist),
                histories_valid=counters["histories_valid"],
                histories_failed=counters["histories_failed"],
                books_valid=counters["books_valid"],
                books_failed=counters["books_failed"],
                observations_eligible=counters["observations_eligible"],
                observations_rejected=counters["observations_rejected"],
                baseline_advanced=0,
                signals_created=0,
                signals_materially_updated=0,
                signals_expired=0,
                storage_ok=False,
                asset_results=tuple(asset_results),
                diagnostic_codes=(OpportunityDiagnosticCode.CYCLE_DEADLINE_EXCEEDED,),
                failure_counters={**failure_counters, "cycle_deadline": 1},
                clob_attempts_used=budget_snap.clob_attempts_used,
                gamma_attempts_used=budget_snap.gamma_attempts_used,
                budget_exhausted_assets=budget_snap.clob_exhausted_count,
                deadline_aborted_before_commit=True,
            )

        storage_ok = True
        try:
            commit_scanner_cycle(
                self.settings,
                asset_writes=writes,
                scanner_state=scanner_state,
                now=current,
            )
        except Exception:
            storage_ok = False
            failure_counters["storage"] = failure_counters.get("storage", 0) + 1
            return ScannerCycleResult(
                cycle_id=cycle_id,
                discovery_status=ResultStatus.COMPLETE,
                markets_discovered=len(discovery.markets),
                outcomes_shortlisted=len(shortlist),
                histories_valid=counters["histories_valid"],
                histories_failed=counters["histories_failed"],
                books_valid=counters["books_valid"],
                books_failed=counters["books_failed"],
                observations_eligible=counters["observations_eligible"],
                observations_rejected=counters["observations_rejected"],
                baseline_advanced=0,
                signals_created=0,
                signals_materially_updated=0,
                signals_expired=0,
                storage_ok=False,
                asset_results=tuple(asset_results),
                diagnostic_codes=(OpportunityDiagnosticCode.STORAGE_FAILURE,),
                failure_counters=failure_counters,
                clob_attempts_used=budget_snap.clob_attempts_used,
                gamma_attempts_used=budget_snap.gamma_attempts_used,
                budget_exhausted_assets=budget_snap.clob_exhausted_count,
            )

        delivery_uids: list[str] = []
        for write in writes:
            for signal in write.signals_to_insert:
                delivery_uids.append(signal.opportunity_uid)

        return ScannerCycleResult(
            cycle_id=cycle_id,
            discovery_status=ResultStatus.COMPLETE,
            markets_discovered=len(discovery.markets),
            outcomes_shortlisted=len(shortlist),
            histories_valid=counters["histories_valid"],
            histories_failed=counters["histories_failed"],
            books_valid=counters["books_valid"],
            books_failed=counters["books_failed"],
            observations_eligible=counters["observations_eligible"],
            observations_rejected=counters["observations_rejected"],
            baseline_advanced=counters["baseline_advanced"],
            signals_created=counters["signals_created"],
            signals_materially_updated=counters["signals_materially_updated"],
            signals_expired=counters["signals_expired"],
            storage_ok=storage_ok,
            asset_results=tuple(asset_results),
            failure_counters=failure_counters,
            delivery_signal_uids=tuple(delivery_uids),
            clob_attempts_used=budget_snap.clob_attempts_used,
            gamma_attempts_used=budget_snap.gamma_attempts_used,
            budget_exhausted_assets=budget_snap.clob_exhausted_count,
        )

    def _apply_book_budgets(
        self, shortlist: tuple[OutcomeCandidate, ...]
    ) -> tuple[tuple[OutcomeCandidate, ...], int]:
        """Cap normal/live book finalists; never silently exceed CLOB book budget.

        Each LIVE_STRICT finalist requires two book snapshots (confirm). Combined
        book fetches are hard-capped at LOCAL_CLOB_REQUEST_CAP_PER_CYCLE.
        """
        from app.opportunity_intel.versions import LOCAL_CLOB_REQUEST_CAP_PER_CYCLE

        kept: list[OutcomeCandidate] = []
        normal_kept = 0
        live_kept = 0
        skipped = 0
        for candidate in shortlist:
            if candidate.mode == ObservationMode.LIVE_STRICT:
                if live_kept >= self.max_live_books_per_cycle:
                    skipped += 1
                    continue
                projected = normal_kept + 2 * (live_kept + 1)
                if projected > LOCAL_CLOB_REQUEST_CAP_PER_CYCLE:
                    skipped += 1
                    continue
                live_kept += 1
            else:
                if normal_kept >= self.max_books_per_cycle:
                    skipped += 1
                    continue
                projected = (normal_kept + 1) + 2 * live_kept
                if projected > LOCAL_CLOB_REQUEST_CAP_PER_CYCLE:
                    skipped += 1
                    continue
                normal_kept += 1
            kept.append(candidate)
        return tuple(kept), skipped

    def _evaluate_candidate(
        self, candidate: OutcomeCandidate, now: datetime
    ) -> dict:
        mode = candidate.mode
        history = self.history_client.fetch(
            candidate.asset_id,
            start_at=now - timedelta(hours=2),
            end_at=now,
        )
        movement = select_movement(history, mode=mode, now=now)
        history_ok = (
            movement.status == ResultStatus.COMPLETE
            and movement.freshness == FreshnessStatus.FRESH
        )
        diagnostics: list[OpportunityDiagnosticCode] = []
        book: ValidatedOrderBook | None = None
        confirmed: ValidatedOrderBook | None = None
        book_ok = False

        if mode == ObservationMode.LIVE_STRICT:
            def _fetch() -> ValidatedOrderBook | BookAdapterFailure:
                adapted = self.book_client.fetch(candidate.asset_id, mode=mode)
                if isinstance(adapted, BookAdapterFailure):
                    return adapted
                return adapted.book

            live = confirm_strict_live_books(
                asset_id=candidate.asset_id,
                fetch_book=_fetch,
                delay=self.delay,
                delay_seconds=self.live_confirm_delay_seconds,
            )
            if isinstance(live, LiveConfirmationFailure):
                diagnostics.append(live.diagnostic_code)
                book_ok = False
            else:
                assert isinstance(live, LiveConfirmationSuccess)
                book = live.first
                confirmed = live.second
                book_ok = True
        else:
            adapted = self.book_client.fetch(candidate.asset_id, mode=mode)
            if isinstance(adapted, BookAdapterFailure):
                diagnostics.append(adapted.diagnostic_code)
            else:
                book = adapted.book
                book_ok = True

        observation_result = None
        if history_ok and book is not None and book_ok:
            observation_result = build_canonical_observation(
                candidate=candidate,
                movement=movement,
                book=book,
                mode=mode,
                confirmed_live_book=confirmed,
            )
            if isinstance(observation_result, ObservationRejected):
                diagnostics.extend(observation_result.diagnostic_codes)
        elif not history_ok and movement.diagnostic_code is not None:
            diagnostics.append(movement.diagnostic_code)

        return {
            "movement": movement,
            "history_ok": history_ok,
            "book": book,
            "confirmed": confirmed,
            "book_ok": book_ok,
            "observation_result": observation_result,
            "diagnostics": tuple(dict.fromkeys(diagnostics)),
        }

    def _build_asset_write(
        self,
        *,
        candidate: OutcomeCandidate,
        payload: dict,
        prior: MarketStateRecord | None,
        cycle_id: str,
        now: datetime,
        present_in_discovery: bool,
    ) -> tuple[AssetEvaluationResult, CycleAssetWrite | None]:
        mode = candidate.mode
        movement: MovementResult = payload["movement"]
        observation_result = payload["observation_result"]
        fingerprint = identity_fingerprint(
            condition_id=candidate.condition_id,
            market_id=candidate.market_id,
            event_id=candidate.event_id,
            outcome=candidate.outcome,
        )
        eligible = isinstance(observation_result, ObservationAccepted)
        observation_valid = eligible or (
            payload["history_ok"]
            and payload["book_ok"]
            and isinstance(observation_result, ObservationRejected)
        )
        # Valid observation for baseline = accepted OR rejected-with-complete inputs.
        # Baseline requires "valid history/book/observation" — use accepted only for
        # advancing, matching "valid observation required".
        baseline_valid = eligible

        observed_at = now
        if eligible:
            observed_at = observation_result.observation.observed_at
        elif payload.get("book") is not None:
            observed_at = datetime.fromtimestamp(
                payload["book"].quote_at, tz=timezone.utc
            )

        baseline_decision = advance_baseline(
            asset_id=candidate.asset_id,
            mode=mode,
            prior=prior,
            cycle_id=cycle_id,
            observed_at=observed_at,
            observation_valid=baseline_valid,
            identity_fingerprint=fingerprint,
            now=now,
            present_in_discovery=present_in_discovery,
        )

        active_signals = load_active_signals_for_asset(self.settings, candidate.asset_id)
        market_closed = candidate.end_at <= now
        complete_below_floor = (
            not eligible
            and payload["history_ok"]
            and payload["book_ok"]
            and isinstance(observation_result, ObservationRejected)
        )

        became_ready = baseline_decision.became_ready
        was_eligible = bool(prior.was_eligible_at_baseline) if prior else False
        if became_ready and eligible:
            was_eligible = True
        allow_new = (
            baseline_decision.baseline.ready
            and not became_ready
            and not was_eligible
        )

        score = (
            observation_result.score
            if isinstance(observation_result, (ObservationAccepted, ObservationRejected))
            else None
        )
        reasons = observation_result.reason_codes if eligible else ()
        source_at = (
            observation_result.observation.observed_at if eligible else observed_at
        )

        key_direction = direction_from_movement(movement) if eligible else None
        episode_seq = 1
        if key_direction is not None:
            key = build_opportunity_key(
                condition_id=candidate.condition_id,
                asset_id=candidate.asset_id,
                direction=key_direction,
                mode=mode,
            )
            episode_seq = next_episode_sequence(self.settings, key)

        episode = decide_episode_actions(
            mode=mode,
            now=now,
            condition_id=candidate.condition_id,
            asset_id=candidate.asset_id,
            end_at=candidate.end_at,
            baseline_ready=baseline_decision.baseline.ready,
            allow_new_episode=allow_new,
            prior=prior,
            eligible=eligible,
            complete_below_floor=complete_below_floor,
            score=score,
            reason_codes=reasons,
            movement=movement,
            source_observed_at=source_at,
            active_signals=active_signals,
            market_closed=market_closed,
            next_episode_sequence=episode_seq,
        )

        if became_ready and eligible:
            final_was_eligible = True
        else:
            final_was_eligible = episode.was_eligible_at_baseline

        signals_to_insert = (
            (episode.create_signal,) if episode.create_signal is not None else ()
        )
        write = CycleAssetWrite(
            baseline=baseline_decision.baseline,
            condition_id=candidate.condition_id,
            event_id=candidate.event_id,
            market_id=candidate.market_id,
            event_slug=candidate.event_slug,
            question=candidate.question,
            outcome=candidate.outcome,
            category_ids=candidate.category_ids,
            metadata_at=candidate.metadata_at,
            end_at=candidate.end_at,
            active=not market_closed,
            observation=(
                observation_result.observation if eligible else None
            ),
            bucket_at=(
                observation_result.observation.observed_at if eligible else None
            ),
            canonical_score=(observation_result.score if eligible else None),
            score_version=OPPORTUNITY_SCORE_VERSION,
            execution_metrics=(
                observation_result.allocation_metrics if eligible else ()
            ),
            last_midpoint=(
                observation_result.observation.midpoint if eligible else (
                    prior.last_midpoint if prior else None
                )
            ),
            last_score=(
                observation_result.score if eligible else (
                    prior.last_score if prior else None
                )
            ),
            safety_policy_version=SAFETY_POLICY_VERSION,
            below_threshold_streak=episode.below_threshold_streak,
            was_eligible_at_baseline=final_was_eligible,
            last_direction=(
                episode.create_signal.direction
                if episode.create_signal is not None
                else (prior.last_direction if prior else None)
            ),
            pending_live_direction=episode.pending_live_direction,
            last_absolute_move_pp=(
                strongest_absolute_move_pp(movement)
                if payload["history_ok"]
                else (prior.last_absolute_move_pp if prior else None)
            ),
            last_signal_at=episode.last_signal_at,
            identity_fingerprint=fingerprint,
            baseline_reset_code=baseline_decision.reset_code,
            signals_to_expire=episode.expire_uids,
            signals_to_insert=signals_to_insert,
        )
        result = AssetEvaluationResult(
            asset_id=candidate.asset_id,
            mode=mode,
            eligible=eligible,
            baseline_advanced=baseline_decision.advanced,
            signal_created=episode.create_signal is not None and not episode.material_updated,
            signal_material_updated=episode.material_updated,
            signal_expired=bool(episode.expire_uids),
            diagnostic_codes=payload["diagnostics"]
            + ((baseline_decision.reset_code,) if baseline_decision.reset_code else ()),
        )
        return result, write

    def _absence_write(
        self, *, prior: MarketStateRecord, cycle_id: str, now: datetime
    ) -> CycleAssetWrite | None:
        decision = advance_baseline(
            asset_id=prior.asset_id,
            mode=prior.mode,
            prior=prior,
            cycle_id=cycle_id,
            observed_at=prior.baseline.last_observed_at or now,
            observation_valid=False,
            identity_fingerprint=prior.identity_fingerprint or prior.asset_id,
            now=now,
            present_in_discovery=False,
        )
        if decision.reset_code is None:
            return None
        return CycleAssetWrite(
            baseline=decision.baseline,
            condition_id=prior.condition_id,
            event_id=prior.event_id,
            market_id=prior.market_id,
            event_slug=prior.event_slug,
            question=prior.question,
            outcome=prior.outcome,
            category_ids=prior.category_ids,
            metadata_at=prior.metadata_at,
            end_at=prior.end_at,
            active=False,
            observation=None,
            bucket_at=None,
            canonical_score=None,
            score_version=OPPORTUNITY_SCORE_VERSION,
            execution_metrics=(),
            last_midpoint=prior.last_midpoint,
            last_score=prior.last_score,
            safety_policy_version=SAFETY_POLICY_VERSION,
            below_threshold_streak=0,
            was_eligible_at_baseline=False,
            last_direction=None,
            pending_live_direction=None,
            last_absolute_move_pp=prior.last_absolute_move_pp,
            last_signal_at=prior.last_signal_at,
            identity_fingerprint=prior.identity_fingerprint or prior.asset_id,
            baseline_reset_code=decision.reset_code,
            signals_to_expire=(),
            signals_to_insert=(),
        )
