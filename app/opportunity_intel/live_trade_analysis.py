"""Fresh public-data assembly for V2.2.9 trade-candidate cards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.config.settings import Settings
from app.copy_intel.order_book import allocate_buy
from app.database.opportunity_storage import PersistedObservationBundle
from app.opportunity_intel.allocation import execution_metrics_from_allocation
from app.opportunity_intel.book import (
    BookAdapterFailure,
    BookAdapterSuccess,
    PublicOrderBookClient,
)
from app.opportunity_intel.discovery import (
    ResultStatus,
    ValidatedMarket,
    validate_gamma_market_for_tradeability,
)
from app.opportunity_intel.history import (
    FreshnessStatus,
    MovementResult,
    PublicPriceHistoryClient,
    select_movement,
)
from app.opportunity_intel.models import OpportunityDiagnosticCode, OpportunitySignal
from app.opportunity_intel.payload import DeliveryCardPayload, build_delivery_card_payload
from app.opportunity_intel.trade_candidate import (
    FeeSchedule,
    LiveMatchState,
    MarketEvidence,
    ResolutionEvidence,
    TradeCandidateDecision,
    build_trade_candidate,
    derive_target_price,
)
from app.polymarket.client import PolymarketClient

_VS_RE = re.compile(r"\s+(?:vs\.?|v\.)\s+", re.IGNORECASE)
_SCORE_RE = re.compile(r"(?<!\d)(\d{1,3})\s*[-:]\s*(\d{1,3})(?!\d)")
_FINAL_STATUSES = frozenset({"final", "finished", "ended", "ft", "complete", "completed"})


@dataclass(frozen=True)
class LiveTradeSnapshot:
    raw_event: dict[str, Any]
    raw_market: dict[str, Any]
    validated_market: ValidatedMarket
    movement: MovementResult
    book: Any
    adapted_book: BookAdapterSuccess
    resolution: ResolutionEvidence
    live_state: LiveMatchState | None
    fee_schedule: FeeSchedule
    evidence: tuple[MarketEvidence, ...]


@dataclass(frozen=True)
class LiveTradeAnalysis:
    payload: DeliveryCardPayload
    decision: TradeCandidateDecision
    snapshot: LiveTradeSnapshot


@dataclass(frozen=True)
class LiveTradeAnalysisFailure:
    diagnostic_code: OpportunityDiagnosticCode
    detail: str


def fetch_live_trade_snapshot(
    settings: Settings,
    *,
    signal: OpportunitySignal,
    bundle: PersistedObservationBundle,
    now: datetime,
    gamma_client: Any | None = None,
    history_client: Any | None = None,
    book_client: Any | None = None,
    evidence: Iterable[MarketEvidence] = (),
) -> LiveTradeSnapshot | LiveTradeAnalysisFailure:
    """Fetch exact current Gamma/history/book facts once for one signal."""
    clock = now.astimezone(timezone.utc)
    gamma = gamma_client if gamma_client is not None else PolymarketClient(settings)
    history_reader = (
        history_client if history_client is not None else PublicPriceHistoryClient(settings)
    )
    book_reader = book_client if book_client is not None else PublicOrderBookClient(settings)

    try:
        raw_markets = gamma.get_json(
            "/markets", params={"condition_ids": bundle.condition_id}
        )
    except Exception:  # noqa: BLE001 - provider faults are fail-closed
        return LiveTradeAnalysisFailure(
            OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED, "gamma_market_request"
        )
    raw_market = _first_dict(raw_markets)
    if raw_market is None:
        return LiveTradeAnalysisFailure(
            OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID, "gamma_market_shape"
        )

    raw_event = _embedded_event(raw_market)
    if raw_event is None or not _embedded_event_complete(raw_event, raw_market):
        try:
            event_payload = gamma.get_json("/events", params={"slug": bundle.event_slug})
        except Exception:  # noqa: BLE001
            return LiveTradeAnalysisFailure(
                OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED, "gamma_event_request"
            )
        raw_event = _first_dict(event_payload)
    if raw_event is None:
        return LiveTradeAnalysisFailure(
            OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID, "gamma_event_shape"
        )
    if str(raw_event.get("id")) != str(bundle.event_id):
        return LiveTradeAnalysisFailure(
            OpportunityDiagnosticCode.LIVE_DATA_INCONSISTENT, "event_identity"
        )

    validated, diagnostic = validate_gamma_market_for_tradeability(
        raw_event, raw_market, now=clock
    )
    if diagnostic is not None or validated is None:
        return LiveTradeAnalysisFailure(
            diagnostic.code if diagnostic else OpportunityDiagnosticCode.MARKET_STATUS_INVALID,
            "gamma_tradeability",
        )
    if signal.asset_id not in {item.asset_id for item in validated.outcomes}:
        return LiveTradeAnalysisFailure(
            OpportunityDiagnosticCode.OUTCOME_TOKEN_MISMATCH, "asset_identity"
        )

    history = history_reader.fetch(
        signal.asset_id,
        start_at=clock - timedelta(hours=2),
        end_at=clock,
    )
    movement = select_movement(history, mode=signal.mode, now=clock)
    if (
        movement.status != ResultStatus.COMPLETE
        or movement.freshness != FreshnessStatus.FRESH
    ):
        return LiveTradeAnalysisFailure(
            movement.diagnostic_code or OpportunityDiagnosticCode.HISTORY_MISSING,
            "fresh_history",
        )

    adapted = book_reader.fetch(signal.asset_id, mode=signal.mode)
    if isinstance(adapted, BookAdapterFailure):
        return LiveTradeAnalysisFailure(adapted.diagnostic_code, "fresh_book")
    assert isinstance(adapted, BookAdapterSuccess)

    resolution = _resolution_evidence(raw_event, raw_market)
    fee_schedule = _fee_schedule(raw_market)
    if isinstance(fee_schedule, LiveTradeAnalysisFailure):
        return fee_schedule

    live_state = _live_state(
        raw_event,
        event_slug=bundle.event_slug,
        event_title=str(raw_event.get("title") or bundle.question),
        outcome=bundle.outcome,
        observed_at=clock,
    )
    return LiveTradeSnapshot(
        raw_event=raw_event,
        raw_market=raw_market,
        validated_market=validated,
        movement=movement,
        book=adapted.book,
        adapted_book=adapted,
        resolution=resolution,
        live_state=live_state,
        fee_schedule=fee_schedule,
        evidence=tuple(evidence),
    )


def analyze_live_trade_snapshot(
    *,
    signal: OpportunitySignal,
    bundle: PersistedObservationBundle,
    snapshot: LiveTradeSnapshot,
    allocation_usd: Decimal,
    maximum_slippage: Decimal,
    now: datetime,
) -> LiveTradeAnalysis | LiveTradeAnalysisFailure:
    """Apply one user's paper allocation to an already-fetched live snapshot."""
    execution = allocate_buy(snapshot.book, float(allocation_usd))
    entry_metric = execution_metrics_from_allocation(
        execution,
        asset_id=signal.asset_id,
        observed_at=snapshot.adapted_book.book_at,
        allocation_usd=allocation_usd,
    )
    if (
        not entry_metric.fully_executable
        or entry_metric.execution_vwap is None
        or entry_metric.slippage is None
    ):
        return LiveTradeAnalysisFailure(
            OpportunityDiagnosticCode.INSUFFICIENT_DEPTH, "entry_allocation"
        )

    original_strongest = max(
        abs(bundle.move_5m), abs(bundle.move_15m), abs(bundle.move_1h)
    )
    target_hint = derive_target_price(
        anchor_price=bundle.midpoint - bundle.move_1h,
        strongest_move=original_strongest,
    )
    decision = build_trade_candidate(
        market_id=bundle.market_id,
        event_slug=bundle.event_slug,
        question=bundle.question,
        outcome=bundle.outcome,
        category_ids=bundle.category_ids,
        mode=signal.mode,
        end_at=bundle.end_at,
        now=now,
        resolution=snapshot.resolution,
        live_state=snapshot.live_state,
        evidence=snapshot.evidence,
        movement=snapshot.movement,
        book=snapshot.book,
        entry_execution=entry_metric,
        fee_schedule=snapshot.fee_schedule,
        maximum_slippage=maximum_slippage,
        target_price_hint=target_hint,
    )
    movement = snapshot.movement
    assert movement.latest_at is not None
    payload = build_delivery_card_payload(
        opportunity_uid=signal.opportunity_uid,
        signal_at=signal.signal_at,
        wave_at=now,
        signal_family=signal.family,
        question=bundle.question,
        outcome=bundle.outcome,
        event_slug=bundle.event_slug,
        mode=signal.mode,
        canonical_score=signal.score,
        reason_codes=signal.reason_codes,
        direction=signal.direction,
        move_5m=(movement.change_5m_pp or Decimal("0")) / Decimal("100"),
        move_15m=(movement.change_15m_pp or Decimal("0")) / Decimal("100"),
        move_1h=(movement.change_1h_pp or Decimal("0")) / Decimal("100"),
        best_bid=snapshot.adapted_book.best_bid,
        best_ask=snapshot.adapted_book.best_ask,
        midpoint=snapshot.adapted_book.midpoint,
        absolute_spread=snapshot.adapted_book.absolute_spread,
        relative_spread=snapshot.adapted_book.relative_spread,
        allocation_usd=allocation_usd,
        execution_vwap=entry_metric.execution_vwap,
        slippage=entry_metric.slippage,
        executable_ask_depth_usd=snapshot.adapted_book.visible_ask_depth_usd,
        volume_24h_usd=bundle.volume_24h_usd,
        liquidity_usd=bundle.liquidity_usd,
        end_at=bundle.end_at,
        source_observed_at=snapshot.adapted_book.book_at,
    )
    return LiveTradeAnalysis(payload=payload, decision=decision, snapshot=snapshot)


def _first_dict(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return None


def _embedded_event(raw_market: dict[str, Any]) -> dict[str, Any] | None:
    events = raw_market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return events[0]
    return None


def _event_has_rules(raw_event: dict[str, Any], raw_market: dict[str, Any]) -> bool:
    return bool(
        raw_market.get("description")
        or raw_market.get("rules")
        or raw_event.get("description")
    )


def _embedded_event_complete(
    raw_event: dict[str, Any], raw_market: dict[str, Any]
) -> bool:
    if not _event_has_rules(raw_event, raw_market):
        return False
    tags = raw_event.get("tags")
    if not isinstance(tags, list) or not tags:
        return False
    slugs = {
        str(tag.get("slug") or "").casefold()
        for tag in tags
        if isinstance(tag, dict)
    }
    if slugs & {"sports", "esports", "e-sports", "games"}:
        return isinstance(raw_event.get("live"), bool)
    return True


def _resolution_evidence(
    raw_event: dict[str, Any], raw_market: dict[str, Any]
) -> ResolutionEvidence:
    rules = str(
        raw_market.get("description")
        or raw_market.get("rules")
        or raw_market.get("resolutionRules")
        or raw_event.get("description")
        or ""
    )
    source = str(
        raw_market.get("resolutionSource")
        or raw_event.get("resolutionSource")
        or ""
    )
    return ResolutionEvidence(rules_text=rules, source_url=source)


def _fee_schedule(raw_market: dict[str, Any]) -> FeeSchedule | LiveTradeAnalysisFailure:
    enabled = raw_market.get("feesEnabled")
    if enabled is False:
        return FeeSchedule(enabled=False, rate=Decimal("0"))
    raw = raw_market.get("feeSchedule")
    if enabled is not True or not isinstance(raw, dict):
        return LiveTradeAnalysisFailure(
            OpportunityDiagnosticCode.MARKET_NUMERIC_INVALID, "fee_schedule_missing"
        )
    try:
        rate = Decimal(str(raw.get("rate")))
        exponent = int(raw.get("exponent"))
        return FeeSchedule(
            enabled=True,
            rate=rate,
            exponent=exponent,
            taker_only=bool(raw.get("takerOnly", True)),
        )
    except (InvalidOperation, TypeError, ValueError):
        return LiveTradeAnalysisFailure(
            OpportunityDiagnosticCode.MARKET_NUMERIC_INVALID, "fee_schedule_invalid"
        )


def _live_state(
    raw_event: dict[str, Any],
    *,
    event_slug: str,
    event_title: str,
    outcome: str,
    observed_at: datetime,
) -> LiveMatchState | None:
    live = raw_event.get("live")
    score = raw_event.get("score")
    if not isinstance(live, bool) and not isinstance(score, str):
        return None
    status = str(raw_event.get("gameStatus") or "").strip()
    ended = bool(raw_event.get("ended")) or status.casefold() in _FINAL_STATUSES
    return LiveMatchState(
        event_slug=event_slug,
        observed_at=observed_at,
        live=live is True,
        ended=ended,
        score=score.strip() if isinstance(score, str) and score.strip() else None,
        period=_optional_text(raw_event.get("period") or status),
        elapsed=_optional_text(raw_event.get("elapsed")),
        outcome_leading=derive_outcome_leading(event_title, outcome, score),
    )


def derive_outcome_leading(title: str, outcome: str, score: Any) -> bool | None:
    """Map an official two-side score to the exact named market outcome."""
    if not isinstance(score, str):
        return None
    teams = _VS_RE.split(title, maxsplit=1)
    if len(teams) != 2:
        return None
    scores = _SCORE_RE.findall(score)
    if not scores:
        return None
    left, right = (int(value) for value in scores[-1])
    if left == right:
        return None
    normalized_outcome = _normalize_name(outcome)
    first = _normalize_name(teams[0].rsplit(":", 1)[-1])
    second = _normalize_name(teams[1].split("(", 1)[0].split(" - ", 1)[0])
    if normalized_outcome == first or normalized_outcome in first or first in normalized_outcome:
        return left > right
    if normalized_outcome == second or normalized_outcome in second or second in normalized_outcome:
        return right > left
    return None


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = [
    "LiveTradeAnalysis",
    "LiveTradeAnalysisFailure",
    "LiveTradeSnapshot",
    "analyze_live_trade_snapshot",
    "derive_outcome_leading",
    "fetch_live_trade_snapshot",
]
