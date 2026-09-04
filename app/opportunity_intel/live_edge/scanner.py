"""Live Edge cycle: discover → evaluate → per-user book/EV/decision. Analysis only."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from app.config.settings import Settings
from app.copy_intel.order_book import ValidatedOrderBook
from app.database import live_edge_storage, opportunity_storage
from app.opportunity_intel.book import BookAdapterFailure, BookAdapterSuccess, PublicOrderBookClient
from app.opportunity_intel.live_edge.classify import classify_market
from app.opportunity_intel.live_edge.decisions import decide
from app.opportunity_intel.live_edge.discovery import (
    EVAL_QUEUE_TOTAL_CAP,
    LiveEdgeDiscoveryResult,
    discover_active_markets,
    harvest_live_snapshots,
    select_evaluation_queue,
)
from app.opportunity_intel.live_edge.economics import executable_quote_from_book
from app.opportunity_intel.live_edge.evaluators import evaluate_market
from app.opportunity_intel.live_edge.formatting import card_payload_json
from app.opportunity_intel.live_edge.models import (
    DiscoveredMarket,
    LiveEdgeDecision,
    LiveEdgeDecisionResult,
    MatchStatus,
)
from app.opportunity_intel.live_edge.providers import LiveStateProvider
from app.opportunity_intel.live_edge.wait_monitor import WaitFire, check_wait_triggers
from app.opportunity_intel.models import ObservationMode
from app.opportunity_intel.settings import OpportunityScannerSettings

logger = logging.getLogger(__name__)

LIVE_EDGE_CLOB_CAP = 40
LIVE_EDGE_EVAL_CAP = EVAL_QUEUE_TOTAL_CAP


@dataclass(frozen=True)
class LiveEdgeCycleResult:
    enabled_users: int
    markets_discovered: int
    evaluations: int
    decisions: int
    buy_now: int
    wait: int
    too_late: int
    skipped: int
    truncated_discovery: bool
    sports_only_discovery: bool
    wait_fires: tuple[WaitFire, ...] = ()
    watch: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)
    skip_reason_counts: dict[str, int] = field(default_factory=dict)
    rejection_message_counts: dict[str, int] = field(default_factory=dict)
    signal_type_counts: dict[str, int] = field(default_factory=dict)
    live_map_evaluated: int = 0
    live_match_evaluated: int = 0
    near_resolution_evaluated: int = 0
    funnel: dict[str, int] = field(default_factory=dict)


def live_edge_users(settings: Settings) -> tuple[tuple[int, OpportunityScannerSettings], ...]:
    found: list[tuple[int, OpportunityScannerSettings]] = []
    for sub in opportunity_storage.list_subscriptions(settings):
        if sub.enabled and sub.settings.live_edge_enabled:
            found.append((sub.telegram_id, sub.settings))
    return tuple(found)


def run_live_edge_cycle(
    settings: Settings,
    *,
    now: datetime | None = None,
    gamma_client: Any | None = None,
    book_client: PublicOrderBookClient | None = None,
    provider: LiveStateProvider | None = None,
    markets: tuple[DiscoveredMarket, ...] | None = None,
    books: dict[str, ValidatedOrderBook] | None = None,
) -> LiveEdgeCycleResult:
    """Pure analysis cycle. Never places orders."""
    live_edge_storage.initialize_live_edge_schema(settings)
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    users = live_edge_users(settings)
    if not users:
        return LiveEdgeCycleResult(0, 0, 0, 0, 0, 0, 0, 0, False, False, ())

    truncated = False
    sports_only = False
    if markets is None:
        if gamma_client is None:
            from app.polymarket.client import PolymarketClient

            gamma_client = PolymarketClient(settings)
        discovery: LiveEdgeDiscoveryResult = discover_active_markets(gamma_client, now=clock)
        markets = discovery.markets
        truncated = discovery.truncated
        sports_only = discovery.sports_only
    if provider is not None:
        source = provider
    else:
        from app.opportunity_intel.live_edge.production import build_production_provider

        source = build_production_provider()
    live_snaps = harvest_live_snapshots(source, now=clock)
    ordered = select_evaluation_queue(
        markets, now=clock, total_cap=LIVE_EDGE_EVAL_CAP, live_snapshots=live_snaps
    )
    evaluations: list[tuple[DiscoveredMarket, LiveEdgeEvaluation]] = []
    for market in ordered:
        identity = classify_market(market)
        evaluation = evaluate_market(market, identity, source, now=clock)
        evaluations.append((market, evaluation))

    buy = wait = too_late = skipped = watch = decisions = 0
    category_counts: dict[str, int] = {}
    skip_reason_counts: dict[str, int] = {}
    rejection_message_counts: dict[str, int] = {}
    signal_type_counts: dict[str, int] = {}
    live_map_evaluated = live_match_evaluated = near_resolution_evaluated = 0
    funnel = {
        "discovered": len(markets),
        "evaluated": len(evaluations),
        "harvested_live_events": len(live_snaps),
        "parseable_participants": 0,
        "provider_snapshots": 0,
        "verified_match": 0,
        "probable_match": 0,
        "no_match": 0,
        "live_state": 0,
        "high_evidence": 0,
        "medium_evidence": 0,
        "low_evidence": 0,
        "executable_liquidity": 0,
        "positive_edge": 0,
        "positive_ev": 0,
    }
    for _market, evaluation in evaluations:
        if len(evaluation.identity.participants) >= 2:
            funnel["parseable_participants"] += 1
        if evaluation.live_state is not None:
            funnel["live_state"] += 1
            funnel["provider_snapshots"] += 1
        elif evaluation.match.status in {
            MatchStatus.VERIFIED_MATCH,
            MatchStatus.PROBABLE_MATCH,
        }:
            funnel["provider_snapshots"] += 1
        if evaluation.match.status == MatchStatus.VERIFIED_MATCH:
            funnel["verified_match"] += 1
        elif evaluation.match.status == MatchStatus.PROBABLE_MATCH:
            funnel["probable_match"] += 1
        elif evaluation.match.status == MatchStatus.NO_MATCH:
            funnel["no_match"] += 1
        if evaluation.evidence_tier.value == "HIGH":
            funnel["high_evidence"] += 1
        elif evaluation.evidence_tier.value == "MEDIUM":
            funnel["medium_evidence"] += 1
        else:
            funnel["low_evidence"] += 1
        category_counts[evaluation.identity.category.value] = (
            category_counts.get(evaluation.identity.category.value, 0) + 1
        )
        signal_type_counts[evaluation.signal_type.value] = (
            signal_type_counts.get(evaluation.signal_type.value, 0) + 1
        )
        if evaluation.signal_type.value == "LIVE_MAP_EDGE":
            live_map_evaluated += 1
        elif evaluation.signal_type.value == "LIVE_MATCH_EDGE":
            live_match_evaluated += 1
        elif evaluation.signal_type.value == "NEAR_RESOLUTION_EDGE":
            near_resolution_evaluated += 1
    clob_used = 0
    book_cache: dict[str, ValidatedOrderBook] = dict(books or {})
    clob_attempts = 0
    clob_failures = 0
    if books is None and book_client is not None:
        prioritized = sorted(
            evaluations,
            key=lambda item: 0 if item[1].live_state is not None else 1,
        )
        for market, _evaluation in prioritized:
            if clob_used >= LIVE_EDGE_CLOB_CAP:
                break
            _side, asset_id = _select_side(market)
            if not asset_id or asset_id in book_cache:
                continue
            clob_used += 1
            clob_attempts += 1
            fetched = book_client.fetch(asset_id, mode=ObservationMode.NORMAL)
            if isinstance(fetched, BookAdapterSuccess):
                book_cache[asset_id] = fetched.book
            else:
                clob_failures += 1
                code = (
                    fetched.source_code
                    if isinstance(fetched, BookAdapterFailure)
                    else "unknown"
                )
                logger.info(
                    "Live Edge CLOB miss token=%s code=%s diagnostic=%s",
                    asset_id[:24],
                    code,
                    getattr(fetched, "diagnostic_code", None),
                )
    funnel["clob_attempts"] = clob_attempts
    funnel["clob_failures"] = clob_failures
    persist_rows: list[dict[str, Any]] = []
    pending_waits: list[tuple[Any, ...]] = []
    for telegram_id, user_settings in users:
        for market, evaluation in evaluations:
            side, asset_id = _select_side(market)
            book = book_cache.get(asset_id)
            quote = None
            if book is not None:
                quote = executable_quote_from_book(
                    book,
                    allocation_usd=user_settings.paper_allocation_usd,
                    side=side,
                    now=clock,
                )
            known_fees = Decimal("0")
            if quote is not None and quote.fully_executable:
                from app.opportunity_intel.live_edge.fees import fetch_taker_fee_usd

                fee_usd, _note = fetch_taker_fee_usd(
                    asset_id,
                    capital_usd=quote.allocation_usd,
                    executable_price=quote.vwap,
                )
                if fee_usd is not None:
                    known_fees = fee_usd
            result = decide(evaluation, quote, now=clock, known_fees_usd=known_fees)
            decisions += 1
            if quote is not None:
                funnel["clob_quotes_attempted"] = funnel.get("clob_quotes_attempted", 0) + 1
                if quote.fully_executable:
                    funnel["executable_liquidity"] = funnel.get("executable_liquidity", 0) + 1
            if result.edge_pp is not None and result.edge_pp > 0:
                funnel["positive_edge"] = funnel.get("positive_edge", 0) + 1
            if result.expected_value_usd is not None and result.expected_value_usd > 0:
                funnel["positive_ev"] = funnel.get("positive_ev", 0) + 1
            if result.decision == LiveEdgeDecision.BUY_NOW:
                buy += 1
            elif result.decision == LiveEdgeDecision.WAIT:
                wait += 1
            elif result.decision == LiveEdgeDecision.WATCH:
                watch += 1
            elif result.decision == LiveEdgeDecision.TOO_LATE:
                too_late += 1
            else:
                skipped += 1
                reason_key = (
                    result.skip_reason.value
                    if result.skip_reason is not None
                    else "unspecified"
                )
                skip_reason_counts[reason_key] = skip_reason_counts.get(reason_key, 0) + 1
                if result.decision_reasons:
                    message = result.decision_reasons[0]
                    rejection_message_counts[message] = (
                        rejection_message_counts.get(message, 0) + 1
                    )
            persist_rows.append(
                _signal_row(
                    telegram_id=telegram_id,
                    market=market,
                    asset_id=asset_id,
                    result=result,
                    now=clock,
                )
            )
            if result.decision == LiveEdgeDecision.WAIT and result.wait_trigger is not None:
                pending_waits.append(
                    (
                        _signal_uid(
                            telegram_id,
                            market.market_id,
                            asset_id,
                            result.evaluation.signal_type.value,
                        ),
                        market.market_id,
                        asset_id,
                        telegram_id,
                        result.wait_trigger,
                    )
                )
    if persist_rows:
        live_edge_storage.upsert_signals(settings, persist_rows)
    for uid, market_id, asset_id, telegram_id, trigger in pending_waits:
        live_edge_storage.upsert_wait(
            settings,
            wait_uid=f"wait:{uid}",
            signal_uid=uid,
            market_id=market_id,
            asset_id=asset_id,
            telegram_id=telegram_id,
            trigger=trigger,
            created_at=clock,
        )
    probabilities = {}
    for _market, evaluation in evaluations:
        if evaluation.probability is not None:
            _side, asset_id = _select_side(_market)
            probabilities[asset_id] = evaluation.probability
    maps = {}
    for _market, evaluation in evaluations:
        if evaluation.live_state is not None and evaluation.live_state.current_map is not None:
            maps[_market.market_id] = evaluation.live_state.current_map
    fires = check_wait_triggers(
        settings,
        quotes=book_cache,
        probabilities=probabilities,
        current_maps=maps,
        now=clock,
    )
    return LiveEdgeCycleResult(
        enabled_users=len(users),
        markets_discovered=len(markets),
        evaluations=len(evaluations),
        decisions=decisions,
        buy_now=buy,
        wait=wait,
        too_late=too_late,
        skipped=skipped,
        truncated_discovery=truncated,
        sports_only_discovery=sports_only,
        wait_fires=fires,
        watch=watch,
        category_counts=category_counts,
        skip_reason_counts=skip_reason_counts,
        rejection_message_counts=rejection_message_counts,
        signal_type_counts=signal_type_counts,
        live_map_evaluated=live_map_evaluated,
        live_match_evaluated=live_match_evaluated,
        near_resolution_evaluated=near_resolution_evaluated,
        funnel={
            **funnel,
            "buy": buy,
            "wait": wait,
            "watch": watch,
            "skip": skipped,
            "too_late": too_late,
            "clob_quotes": len(book_cache),
            "clob_attempts": funnel.get("clob_attempts", 0),
            "clob_failures": funnel.get("clob_failures", 0),
        },
    )


def evaluate_for_user(
    market: DiscoveredMarket,
    *,
    user_settings: OpportunityScannerSettings,
    provider: LiveStateProvider,
    book: ValidatedOrderBook | None,
    now: datetime,
) -> LiveEdgeDecisionResult:
    identity = classify_market(market)
    evaluation = evaluate_market(market, identity, provider, now=now)
    side, _asset = _select_side(market)
    quote = None
    if book is not None:
        quote = executable_quote_from_book(
            book,
            allocation_usd=user_settings.paper_allocation_usd,
            side=side,
            now=now,
        )
    return decide(evaluation, quote, now=now)


def _select_side(market: DiscoveredMarket) -> tuple[str, str]:
    yes_asset = None
    no_asset = None
    for name, asset_id in market.outcomes:
        upper = name.strip().upper()
        if upper == "YES":
            yes_asset = asset_id
        elif upper == "NO":
            no_asset = asset_id
    if yes_asset is not None:
        return "YES", yes_asset
    if no_asset is not None:
        return "NO", no_asset
    return "YES", market.outcomes[0][1]


def _signal_row(
    *,
    telegram_id: int,
    market: DiscoveredMarket,
    asset_id: str,
    result: LiveEdgeDecisionResult,
    now: datetime,
) -> dict[str, Any]:
    uid = _signal_uid(telegram_id, market.market_id, asset_id, result.evaluation.signal_type.value)
    evaluation = result.evaluation
    probability = evaluation.probability
    quote = result.quote
    first_wait = now.isoformat() if result.decision == LiveEdgeDecision.WAIT else None
    first_buy = now.isoformat() if result.decision == LiveEdgeDecision.BUY_NOW else None
    provenance = {}
    if evaluation.provenance is not None:
        provenance = {
            "provider": evaluation.provenance.provider,
            "event_identifier": evaluation.provenance.event_identifier,
            "source_locator": evaluation.provenance.source_locator,
            "fetched_at": evaluation.provenance.fetched_at.isoformat(),
            "reliability": evaluation.provenance.reliability,
        }
    return {
        "signal_uid": uid,
        "telegram_id": telegram_id,
        "market_id": market.market_id,
        "asset_id": asset_id,
        "question": market.question,
        "category": evaluation.identity.category.value,
        "signal_type": evaluation.signal_type.value,
        "decision": result.decision.value,
        "side": result.side,
        "conservative_p_low": str(probability.low) if probability else None,
        "conservative_p_high": str(probability.high) if probability else None,
        "executable_price": str(quote.vwap) if quote else None,
        "edge_pp": str(result.edge_pp) if result.edge_pp is not None else None,
        "ev_usd": str(result.expected_value_usd)
        if result.expected_value_usd is not None
        else None,
        "roi": str(result.expected_roi) if result.expected_roi is not None else None,
        "reasons_json": json.dumps(list(result.decision_reasons), ensure_ascii=False),
        "risks_json": json.dumps(list(evaluation.risks), ensure_ascii=False),
        "wait_trigger_json": (
            json.dumps(
                {
                    "description_uk": result.wait_trigger.description_uk,
                    "max_yes_ask": str(result.wait_trigger.max_yes_ask)
                    if result.wait_trigger.max_yes_ask
                    else None,
                    "min_edge_pp": str(result.wait_trigger.min_edge_pp)
                    if result.wait_trigger.min_edge_pp
                    else None,
                    "required_map_win": result.wait_trigger.required_map_win,
                    "require_executable_bankroll": result.wait_trigger.require_executable_bankroll,
                },
                ensure_ascii=False,
            )
            if result.wait_trigger is not None
            else None
        ),
        "provenance_json": json.dumps(provenance, ensure_ascii=False),
        "card_json": card_payload_json(result),
        "first_detected_at": now.isoformat(),
        "first_wait_at": first_wait,
        "first_buy_at": first_buy,
        "best_executable_price": str(quote.vwap) if quote else None,
        "best_edge_pp": str(result.edge_pp) if result.edge_pp is not None else None,
        "best_ev_usd": str(result.expected_value_usd)
        if result.expected_value_usd is not None
        else None,
        "evaluated_at": result.calculated_at.isoformat(),
        "quote_age_seconds": str(quote.quote_age_seconds) if quote else None,
        "live_data_age_seconds": (
            str(evaluation.live_data_age_seconds)
            if evaluation.live_data_age_seconds is not None
            else None
        ),
    }


def _signal_uid(telegram_id: int, market_id: str, asset_id: str, signal_type: str) -> str:
    digest = hashlib.sha256(
        f"{telegram_id}:{market_id}:{asset_id}:{signal_type}".encode("utf-8")
    ).hexdigest()[:24]
    return f"le:{digest}"
