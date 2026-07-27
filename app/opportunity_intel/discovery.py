"""Complete Gamma discovery, strict normalization, and metadata shortlisting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Callable, Protocol

import requests

from app.http.retry import is_retryable_status
from app.opportunity_intel.models import (
    ObservationMode,
    OpportunityDiagnosticCode,
)
from app.opportunity_intel.request_budget import RequestBudgetExhausted
from app.opportunity_intel.settings import (
    BALANCED_MINIMUM_24H_VOLUME_USD,
    BALANCED_MINIMUM_LIQUIDITY_USD,
    BALANCED_MINIMUM_TIME_REMAINING_SECONDS,
    NORMAL_METADATA_MAXIMUM_AGE_SECONDS,
    STRICT_LIVE_MINIMUM_24H_VOLUME_USD,
    STRICT_LIVE_MINIMUM_LIQUIDITY_USD,
    STRICT_LIVE_MINIMUM_TIME_REMAINING_SECONDS,
    STRICT_LIVE_METADATA_MAXIMUM_AGE_SECONDS,
)
from app.polymarket.links import build_validated_market_link

DISCOVERY_CACHE_TTL_SECONDS = 300
DISCOVERY_PAGE_LIMIT = 500
DISCOVERY_MAX_PAGES = 20
SHORTLIST_MAX_OUTCOMES = 100

_CURSOR_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,512}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class ResultStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    EXPIRED = "EXPIRED"


class GammaDiscoveryClient(Protocol):
    def get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any: ...


@dataclass(frozen=True)
class DiscoveryDiagnostic:
    code: OpportunityDiagnosticCode
    event_id: str | None = None
    market_id: str | None = None


@dataclass(frozen=True)
class ValidatedOutcome:
    asset_id: str
    name: str
    price_hint: Decimal | None
    index: int


@dataclass(frozen=True)
class ValidatedMarket:
    event_id: str
    market_id: str
    condition_id: str
    event_slug: str
    question: str
    category_ids: tuple[str, ...]
    mode: ObservationMode
    liquidity_usd: Decimal
    volume_24h_usd: Decimal
    metadata_at: datetime
    end_at: datetime
    outcomes: tuple[ValidatedOutcome, ...]


@dataclass(frozen=True)
class OutcomeCandidate:
    event_id: str
    market_id: str
    condition_id: str
    event_slug: str
    question: str
    category_ids: tuple[str, ...]
    mode: ObservationMode
    liquidity_usd: Decimal
    volume_24h_usd: Decimal
    metadata_at: datetime
    end_at: datetime
    asset_id: str
    outcome: str
    outcome_index: int
    price_hint: Decimal | None


@dataclass(frozen=True)
class DiscoveryResult:
    status: ResultStatus
    markets: tuple[ValidatedMarket, ...]
    diagnostics: tuple[DiscoveryDiagnostic, ...]
    pages_fetched: int
    completed_at: datetime | None
    from_cache: bool = False
    refresh_failure_status: ResultStatus | None = None
    gamma_attempts_this_cycle: int = 0
    gamma_attempts_cumulative: int = 0
    pages_this_cycle: int = 0
    pages_cumulative: int = 0
    session_resumed: bool = False
    session_id: str | None = None
    session_state: ResultStatus | None = None

    @property
    def complete(self) -> bool:
        return self.status == ResultStatus.COMPLETE


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _failure(
    status: ResultStatus,
    code: OpportunityDiagnosticCode,
    *,
    pages_fetched: int,
    gamma_attempts_this_cycle: int = 0,
    gamma_attempts_cumulative: int | None = None,
    pages_this_cycle: int = 0,
    pages_cumulative: int | None = None,
    session_resumed: bool = False,
    session_id: str | None = None,
) -> DiscoveryResult:
    cumulative_attempts = (
        pages_fetched
        if gamma_attempts_cumulative is None
        else gamma_attempts_cumulative
    )
    cumulative_pages = pages_fetched if pages_cumulative is None else pages_cumulative
    return DiscoveryResult(
        status=status,
        markets=(),
        diagnostics=(DiscoveryDiagnostic(code=code),),
        pages_fetched=pages_fetched,
        completed_at=None,
        gamma_attempts_this_cycle=gamma_attempts_this_cycle,
        gamma_attempts_cumulative=cumulative_attempts,
        pages_this_cycle=pages_this_cycle or pages_fetched,
        pages_cumulative=cumulative_pages,
        session_resumed=session_resumed,
        session_id=session_id,
        session_state=status,
    )


def _request_failure_status(exc: BaseException) -> ResultStatus:
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    ):
        return ResultStatus.RETRYABLE_FAILURE
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        status_code = response.status_code if response is not None else None
        if is_retryable_status(status_code):
            return ResultStatus.RETRYABLE_FAILURE
        if status_code is not None and 400 <= status_code < 500:
            return ResultStatus.PERMANENT_FAILURE
        return ResultStatus.RETRYABLE_FAILURE
    return ResultStatus.PERMANENT_FAILURE


def discover_complete_markets(
    client: GammaDiscoveryClient,
    *,
    now: datetime | None = None,
    limit: int = DISCOVERY_PAGE_LIMIT,
    max_pages: int = DISCOVERY_MAX_PAGES,
) -> DiscoveryResult:
    """Fetch and validate one complete active-market universe."""
    if not 1 <= limit <= DISCOVERY_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {DISCOVERY_PAGE_LIMIT}")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    completed_at = _aware_utc(now or _utc_now(), "now")
    raw_events: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages_fetched = 0
    terminated_normally = False

    for _ in range(max_pages):
        params: dict[str, Any] = {
            "closed": "false",
            "limit": limit,
        }
        if cursor is not None:
            params["after_cursor"] = cursor
        try:
            payload = client.get_json("/events/keyset", params=params)
        except RequestBudgetExhausted:
            return _failure(
                ResultStatus.PERMANENT_FAILURE,
                OpportunityDiagnosticCode.GAMMA_BUDGET_EXHAUSTED,
                pages_fetched=pages_fetched,
                gamma_attempts_this_cycle=pages_fetched,
                gamma_attempts_cumulative=pages_fetched,
            )
        except Exception as exc:
            attempts = pages_fetched + 1
            return _failure(
                _request_failure_status(exc),
                OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED,
                pages_fetched=pages_fetched,
                gamma_attempts_this_cycle=attempts,
                gamma_attempts_cumulative=attempts,
                pages_this_cycle=attempts,
            )
        pages_fetched += 1
        if not isinstance(payload, dict) or not isinstance(
            payload.get("events"), list
        ):
            return _failure(
                ResultStatus.PERMANENT_FAILURE,
                OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID,
                pages_fetched=pages_fetched,
            )
        events = payload["events"]
        if any(not isinstance(event, dict) for event in events):
            return _failure(
                ResultStatus.PERMANENT_FAILURE,
                OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID,
                pages_fetched=pages_fetched,
            )
        raw_events.extend(events)
        if not events:
            terminated_normally = True
            break
        if "next_cursor" not in payload or payload["next_cursor"] is None:
            terminated_normally = True
            break
        next_cursor = payload["next_cursor"]
        if (
            not isinstance(next_cursor, str)
            or ".." in next_cursor
            or not _CURSOR_RE.fullmatch(next_cursor)
        ):
            return _failure(
                ResultStatus.PERMANENT_FAILURE,
                OpportunityDiagnosticCode.DISCOVERY_CURSOR_INVALID,
                pages_fetched=pages_fetched,
            )
        if next_cursor in seen_cursors:
            return _failure(
                ResultStatus.PERMANENT_FAILURE,
                OpportunityDiagnosticCode.DISCOVERY_CURSOR_REPEAT,
                pages_fetched=pages_fetched,
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    if not terminated_normally:
        return _failure(
            ResultStatus.PERMANENT_FAILURE,
            OpportunityDiagnosticCode.DISCOVERY_PAGE_CAP,
            pages_fetched=pages_fetched,
        )

    markets: list[ValidatedMarket] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    seen_events: set[str] = set()
    seen_markets: set[str] = set()
    seen_conditions: set[str] = set()
    seen_assets: set[str] = set()

    for raw_event in raw_events:
        raw_event_id = raw_event.get("id")
        event_id = _validated_identifier(raw_event_id)
        if event_id is None:
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID
                )
            )
            continue
        if event_id in seen_events:
            return _failure(
                ResultStatus.PERMANENT_FAILURE,
                OpportunityDiagnosticCode.DUPLICATE_EVENT,
                pages_fetched=pages_fetched,
            )
        seen_events.add(event_id)
        event_markets, event_diagnostics = _validate_event(raw_event, event_id)
        fatal_codes = {
            OpportunityDiagnosticCode.DUPLICATE_MARKET,
            OpportunityDiagnosticCode.DUPLICATE_ASSET,
        }
        fatal = next(
            (
                diagnostic
                for diagnostic in event_diagnostics
                if diagnostic.code in fatal_codes
            ),
            None,
        )
        if fatal is not None:
            return _failure(
                ResultStatus.PERMANENT_FAILURE,
                fatal.code,
                pages_fetched=pages_fetched,
            )
        diagnostics.extend(event_diagnostics)
        for market in event_markets:
            if market.market_id in seen_markets or market.condition_id in seen_conditions:
                return _failure(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.DUPLICATE_MARKET,
                    pages_fetched=pages_fetched,
                )
            asset_ids = [outcome.asset_id for outcome in market.outcomes]
            if len(set(asset_ids)) != len(asset_ids) or any(
                asset_id in seen_assets for asset_id in asset_ids
            ):
                return _failure(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.DUPLICATE_ASSET,
                    pages_fetched=pages_fetched,
                )
            seen_markets.add(market.market_id)
            seen_conditions.add(market.condition_id)
            seen_assets.update(asset_ids)
            markets.append(market)

    markets.sort(key=lambda market: (market.event_id, market.market_id))
    return DiscoveryResult(
        status=ResultStatus.COMPLETE,
        markets=tuple(markets),
        diagnostics=tuple(diagnostics),
        pages_fetched=pages_fetched,
        completed_at=completed_at,
        gamma_attempts_this_cycle=pages_fetched,
        gamma_attempts_cumulative=pages_fetched,
        pages_this_cycle=pages_fetched,
        pages_cumulative=pages_fetched,
        session_state=ResultStatus.COMPLETE,
    )


def _validate_event(
    raw_event: dict[str, Any], event_id: str
) -> tuple[list[ValidatedMarket], list[DiscoveryDiagnostic]]:
    diagnostics: list[DiscoveryDiagnostic] = []
    if (
        raw_event.get("active") is not True
        or raw_event.get("closed") is not False
        or raw_event.get("archived") is not False
        or raw_event.get("resolved") is True
    ):
        return [], [
            DiscoveryDiagnostic(
                code=OpportunityDiagnosticCode.MARKET_STATUS_INVALID,
                event_id=event_id,
            )
        ]
    slug = raw_event.get("slug")
    if build_validated_market_link(slug) is None:
        return [], [
            DiscoveryDiagnostic(
                code=OpportunityDiagnosticCode.LINK_SLUG_INVALID,
                event_id=event_id,
            )
        ]
    category_ids = _category_ids(raw_event.get("tags"))
    if category_ids is None:
        return [], [
            DiscoveryDiagnostic(
                code=OpportunityDiagnosticCode.MARKET_CATEGORY_INVALID,
                event_id=event_id,
            )
        ]
    raw_markets = raw_event.get("markets")
    if not isinstance(raw_markets, list):
        return [], [
            DiscoveryDiagnostic(
                code=OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID,
                event_id=event_id,
            )
        ]

    markets: list[ValidatedMarket] = []
    local_market_ids: set[str] = set()
    for raw_market in raw_markets:
        if not isinstance(raw_market, dict):
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID,
                    event_id=event_id,
                )
            )
            continue
        raw_market_id = _validated_identifier(raw_market.get("id"))
        if raw_market_id is not None and raw_market_id in local_market_ids:
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=OpportunityDiagnosticCode.DUPLICATE_MARKET,
                    event_id=event_id,
                    market_id=raw_market_id,
                )
            )
            continue
        if raw_market_id is not None:
            local_market_ids.add(raw_market_id)
        market, diagnostic = _validate_market(
            raw_event,
            raw_market,
            event_id=event_id,
            event_slug=str(slug),
            category_ids=category_ids,
        )
        if diagnostic is not None:
            diagnostics.append(diagnostic)
            continue
        assert market is not None
        markets.append(market)
    return markets, diagnostics


def _validate_market(
    raw_event: dict[str, Any],
    raw_market: dict[str, Any],
    *,
    event_id: str,
    event_slug: str,
    category_ids: tuple[str, ...],
) -> tuple[ValidatedMarket | None, DiscoveryDiagnostic | None]:
    market_id = _validated_identifier(raw_market.get("id"))
    condition_id = _validated_identifier(raw_market.get("conditionId"))
    if market_id is None or condition_id is None:
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID,
            event_id=event_id,
            market_id=market_id,
        )
    if (
        raw_market.get("active") is not True
        or raw_market.get("closed") is not False
        or raw_market.get("archived") is not False
        or raw_market.get("resolved") is True
        or raw_market.get("enableOrderBook") is not True
        or raw_market.get("acceptingOrders") is not True
    ):
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.MARKET_STATUS_INVALID,
            event_id=event_id,
            market_id=market_id,
        )
    mode = _classify_mode(raw_event, raw_market)
    if mode is None:
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.MARKET_STATUS_INVALID,
            event_id=event_id,
            market_id=market_id,
        )
    question = raw_market.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID,
            event_id=event_id,
            market_id=market_id,
        )
    outcomes = _json_list(raw_market.get("outcomes"))
    tokens = _json_list(raw_market.get("clobTokenIds"))
    if (
        outcomes is None
        or tokens is None
        or len(outcomes) < 2
        or len(outcomes) != len(tokens)
    ):
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.OUTCOME_TOKEN_MISMATCH,
            event_id=event_id,
            market_id=market_id,
        )
    normalized_names: list[str] = []
    normalized_tokens: list[str] = []
    for name, token in zip(outcomes, tokens, strict=True):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            return None, DiscoveryDiagnostic(
                code=OpportunityDiagnosticCode.OUTCOME_TOKEN_MISMATCH,
                event_id=event_id,
                market_id=market_id,
            )
        asset_id = _validated_identifier(token)
        if asset_id is None:
            return None, DiscoveryDiagnostic(
                code=OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID,
                event_id=event_id,
                market_id=market_id,
            )
        normalized_names.append(name.strip())
        normalized_tokens.append(asset_id)
    if len(set(normalized_tokens)) != len(normalized_tokens):
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.DUPLICATE_ASSET,
            event_id=event_id,
            market_id=market_id,
        )

    liquidity = _decimal_nonnegative(
        raw_market.get("liquidityNum", raw_market.get("liquidity"))
    )
    volume_24h = _decimal_nonnegative(
        raw_market.get("volume24hr", raw_market.get("volume24h"))
    )
    if liquidity is None or volume_24h is None:
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.MARKET_NUMERIC_INVALID,
            event_id=event_id,
            market_id=market_id,
        )
    end_at = _datetime_value(raw_market.get("endDate") or raw_event.get("endDate"))
    metadata_at = _datetime_value(
        raw_market.get("updatedAt") or raw_event.get("updatedAt")
    )
    if end_at is None or metadata_at is None:
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.MARKET_TIME_INVALID,
            event_id=event_id,
            market_id=market_id,
        )
    price_values = _json_list(raw_market.get("outcomePrices"))
    price_hints: list[Decimal | None]
    if price_values is None or len(price_values) == 0:
        price_hints = [None] * len(normalized_names)
    elif len(price_values) != len(normalized_names):
        return None, DiscoveryDiagnostic(
            code=OpportunityDiagnosticCode.MARKET_NUMERIC_INVALID,
            event_id=event_id,
            market_id=market_id,
        )
    else:
        price_hints = []
        for value in price_values:
            parsed = _decimal_probability(value)
            if parsed is None:
                return None, DiscoveryDiagnostic(
                    code=OpportunityDiagnosticCode.MARKET_NUMERIC_INVALID,
                    event_id=event_id,
                    market_id=market_id,
                )
            price_hints.append(parsed)

    normalized_outcomes = tuple(
        ValidatedOutcome(
            asset_id=asset_id,
            name=name,
            price_hint=price_hints[index],
            index=index,
        )
        for index, (asset_id, name) in enumerate(
            zip(normalized_tokens, normalized_names, strict=True)
        )
    )
    return (
        ValidatedMarket(
            event_id=event_id,
            market_id=market_id,
            condition_id=condition_id,
            event_slug=event_slug,
            question=question.strip(),
            category_ids=category_ids,
            mode=mode,
            liquidity_usd=liquidity,
            volume_24h_usd=volume_24h,
            metadata_at=metadata_at,
            end_at=end_at,
            outcomes=normalized_outcomes,
        ),
        None,
    )


def shortlist_outcomes(
    markets: tuple[ValidatedMarket, ...] | list[ValidatedMarket],
    *,
    now: datetime,
    strict_live_demand: bool,
    max_candidates: int = SHORTLIST_MAX_OUTCOMES,
) -> tuple[OutcomeCandidate, ...]:
    """Pure activity-first shortlist; no HTTP calls and no scoring."""
    if not 1 <= max_candidates <= SHORTLIST_MAX_OUTCOMES:
        raise ValueError(
            f"max_candidates must be between 1 and {SHORTLIST_MAX_OUTCOMES}"
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = now.astimezone(timezone.utc)
    candidates: list[OutcomeCandidate] = []
    for market in markets:
        if market.mode == ObservationMode.NORMAL:
            metadata_max_age = NORMAL_METADATA_MAXIMUM_AGE_SECONDS
            min_liquidity = BALANCED_MINIMUM_LIQUIDITY_USD
            min_volume = BALANCED_MINIMUM_24H_VOLUME_USD
            min_remaining = BALANCED_MINIMUM_TIME_REMAINING_SECONDS
        elif market.mode == ObservationMode.LIVE_STRICT:
            if not strict_live_demand:
                continue
            metadata_max_age = STRICT_LIVE_METADATA_MAXIMUM_AGE_SECONDS
            min_liquidity = STRICT_LIVE_MINIMUM_LIQUIDITY_USD
            min_volume = STRICT_LIVE_MINIMUM_24H_VOLUME_USD
            min_remaining = STRICT_LIVE_MINIMUM_TIME_REMAINING_SECONDS
        else:  # pragma: no cover - enum exhaustiveness
            continue
        age = (current - market.metadata_at).total_seconds()
        remaining = (market.end_at - current).total_seconds()
        if age < 0 or age > metadata_max_age:
            continue
        if remaining < min_remaining:
            continue
        if market.liquidity_usd < min_liquidity:
            continue
        if market.volume_24h_usd < min_volume:
            continue
        for outcome in market.outcomes:
            if outcome.price_hint is not None and not (
                Decimal("0.05") <= outcome.price_hint <= Decimal("0.95")
            ):
                continue
            candidates.append(
                OutcomeCandidate(
                    event_id=market.event_id,
                    market_id=market.market_id,
                    condition_id=market.condition_id,
                    event_slug=market.event_slug,
                    question=market.question,
                    category_ids=market.category_ids,
                    mode=market.mode,
                    liquidity_usd=market.liquidity_usd,
                    volume_24h_usd=market.volume_24h_usd,
                    metadata_at=market.metadata_at,
                    end_at=market.end_at,
                    asset_id=outcome.asset_id,
                    outcome=outcome.name,
                    outcome_index=outcome.index,
                    price_hint=outcome.price_hint,
                )
            )
    candidates.sort(
        key=lambda item: (
            -item.volume_24h_usd,
            -item.liquidity_usd,
            item.end_at,
            item.event_id,
            item.market_id,
            item.outcome_index,
            item.asset_id,
        )
    )
    return tuple(candidates[:max_candidates])


class CompleteDiscoveryCache:
    """In-memory complete-universe cache plus resumable discovery session."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DISCOVERY_CACHE_TTL_SECONDS,
        session_ttl_seconds: int = 3600,
        session_store: Any | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if session_ttl_seconds < 1:
            raise ValueError("session_ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self._complete: DiscoveryResult | None = None
        self._session: Any | None = None
        self._session_store = session_store

    def get(self, *, now: datetime) -> DiscoveryResult | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        result = self._complete
        if result is None or result.completed_at is None:
            return None
        age = (now.astimezone(timezone.utc) - result.completed_at).total_seconds()
        if age < 0 or age > self.ttl_seconds:
            return None
        return replace(result, from_cache=True)

    def _load_session(self) -> Any | None:
        if self._session is not None:
            return self._session
        store = self._session_store
        if store is None:
            return None
        loaded = store.load_active_session()
        self._session = loaded
        return loaded

    def _save_session(self, session: Any | None) -> None:
        self._session = session
        store = self._session_store
        if store is None:
            return
        if session is None:
            store.clear_session()
        else:
            store.save_session(session)

    def refresh(
        self,
        fetch: Callable[[], DiscoveryResult],
        *,
        now: datetime,
    ) -> DiscoveryResult:
        """One-shot refresh helper used by unit tests and non-resumable callers."""
        result = fetch()
        if result.complete:
            self._complete = replace(
                result,
                from_cache=False,
                refresh_failure_status=None,
            )
            self._save_session(None)
            return self._complete
        cached = self.get(now=now)
        if cached is not None:
            return replace(
                cached,
                diagnostics=cached.diagnostics + result.diagnostics,
                refresh_failure_status=result.status,
                gamma_attempts_this_cycle=result.gamma_attempts_this_cycle,
                gamma_attempts_cumulative=result.gamma_attempts_cumulative,
                pages_this_cycle=result.pages_this_cycle,
                pages_cumulative=result.pages_cumulative,
                session_resumed=result.session_resumed,
                session_id=result.session_id,
                session_state=result.session_state,
            )
        return result

    def advance_from_client(
        self,
        client: GammaDiscoveryClient,
        *,
        now: datetime,
        max_attempts: int = 10,
    ) -> DiscoveryResult:
        """Resume or start discovery; publish COMPLETE universe only when finished."""
        from app.opportunity_intel.discovery_session import advance_discovery_session

        clock = _aware_utc(now, "now")
        cached = self.get(now=clock)
        session = self._load_session()
        # Still-fresh COMPLETE with no rebuild in flight: serve cache only.
        if cached is not None and session is None:
            return cached

        advanced = advance_discovery_session(
            client,
            session,
            now=clock,
            max_attempts=max_attempts,
            ttl_seconds=self.session_ttl_seconds,
        )
        result = advanced.result
        if advanced.published_complete and result.complete:
            self._complete = replace(
                result,
                from_cache=False,
                refresh_failure_status=None,
            )
            self._save_session(None)
            return self._complete

        if result.status in {
            ResultStatus.IN_PROGRESS,
            ResultStatus.RETRYABLE_FAILURE,
        }:
            self._save_session(advanced.session)
            if cached is not None:
                # Do not replace a still-fresh COMPLETE with partial progress.
                return replace(
                    cached,
                    diagnostics=cached.diagnostics + result.diagnostics,
                    refresh_failure_status=result.status,
                    gamma_attempts_this_cycle=result.gamma_attempts_this_cycle,
                    gamma_attempts_cumulative=result.gamma_attempts_cumulative,
                    pages_this_cycle=result.pages_this_cycle,
                    pages_cumulative=result.pages_cumulative,
                    session_resumed=result.session_resumed,
                    session_id=result.session_id,
                    session_state=result.session_state,
                )
            return result

        # Permanent / expired: drop incomplete session; keep fresh COMPLETE if any.
        self._save_session(None)
        if cached is not None:
            return replace(
                cached,
                diagnostics=cached.diagnostics + result.diagnostics,
                refresh_failure_status=result.status,
                gamma_attempts_this_cycle=result.gamma_attempts_this_cycle,
                gamma_attempts_cumulative=result.gamma_attempts_cumulative,
                pages_this_cycle=result.pages_this_cycle,
                pages_cumulative=result.pages_cumulative,
                session_resumed=result.session_resumed,
                session_id=result.session_id,
                session_state=result.session_state,
            )
        return result


def _validated_identifier(value: Any) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value)
    return text if _IDENTIFIER_RE.fullmatch(text) else None


def _category_ids(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    identifiers: set[str] = set()
    for tag in value:
        if not isinstance(tag, dict):
            return None
        candidate = tag.get("id") or tag.get("slug")
        if not isinstance(candidate, (str, int)) or isinstance(candidate, bool):
            return None
        text = str(candidate)
        if not _CATEGORY_RE.fullmatch(text):
            return None
        identifiers.add(text)
    return tuple(sorted(identifiers)) if identifiers else None


def _json_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, list) else None
    if value is None:
        return []
    return None


def _decimal_nonnegative(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _decimal_probability(value: Any) -> Decimal | None:
    parsed = _decimal_nonnegative(value)
    if parsed is None or parsed > 1:
        return None
    return parsed


def _datetime_value(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _classify_mode(
    event: dict[str, Any], market: dict[str, Any]
) -> ObservationMode | None:
    flags = [
        value
        for value in (
            market.get("live"),
            market.get("isLive"),
            event.get("live"),
            event.get("isLive"),
        )
        if isinstance(value, bool)
    ]
    if not flags:
        return None
    return (
        ObservationMode.LIVE_STRICT
        if any(flags)
        else ObservationMode.NORMAL
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must have a UTC offset")
    return value.astimezone(timezone.utc)
