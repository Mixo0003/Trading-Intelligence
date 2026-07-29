"""Resumable complete Gamma discovery across bounded per-cycle attempt budgets."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Iterable
from typing import Any, Protocol

from app.opportunity_intel.discovery import (
    DISCOVERY_PAGE_LIMIT,
    DiscoveryDiagnostic,
    DiscoveryResult,
    GammaDiscoveryClient,
    ResultStatus,
    ValidatedMarket,
    ValidatedOutcome,
    _aware_utc,
    _request_failure_status,
    _validate_event,
    _validated_identifier,
    _CURSOR_RE,
)
from app.opportunity_intel.models import ObservationMode, OpportunityDiagnosticCode
from app.opportunity_intel.request_budget import RequestBudgetExhausted
from app.opportunity_intel.versions import LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE

DISCOVERY_SESSION_VERSION = 1
DEFAULT_DISCOVERY_SESSION_TTL_SECONDS = 21600
# Per-page normalization rejections are informational only; fatal codes return
# immediately. Bound them so a multi-hundred-page session cannot rewrite a
# multi-megabyte JSON blob on every cycle.
MAX_SESSION_DIAGNOSTICS = 200


class DiscoverySessionStore(Protocol):
    def load_active_session(self) -> DiscoverySession | None: ...

    def save_session(self, session: DiscoverySession) -> None: ...

    def clear_session(self, session_id: str | None = None) -> None: ...


@dataclass(frozen=True)
class DiscoverySession:
    """Normalized discovery progress. Never holds raw Gamma page payloads."""

    session_id: str
    version: int
    state: ResultStatus
    started_at: datetime
    expires_at: datetime
    next_cursor: str | None
    seen_cursors: tuple[str, ...]
    pages_completed: int
    attempts_used: int
    markets: tuple[ValidatedMarket, ...]
    seen_event_ids: frozenset[str]
    seen_market_ids: frozenset[str]
    seen_condition_ids: frozenset[str]
    seen_asset_ids: frozenset[str]
    diagnostics: tuple[DiscoveryDiagnostic, ...]
    updated_at: datetime

    def is_expired(self, *, now: datetime) -> bool:
        clock = _aware_utc(now, "now")
        return clock >= self.expires_at.astimezone(timezone.utc)


@dataclass(frozen=True)
class DiscoveryAdvanceResult:
    """One cycle of resumable discovery. Trusted markets only when COMPLETE."""

    result: DiscoveryResult
    session: DiscoverySession | None
    resumed: bool
    pages_this_cycle: int
    attempts_this_cycle: int
    published_complete: bool


def new_discovery_session(
    *,
    now: datetime,
    ttl_seconds: int = DEFAULT_DISCOVERY_SESSION_TTL_SECONDS,
) -> DiscoverySession:
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be positive")
    clock = _aware_utc(now, "now")
    return DiscoverySession(
        session_id=uuid.uuid4().hex,
        version=DISCOVERY_SESSION_VERSION,
        state=ResultStatus.NOT_STARTED,
        started_at=clock,
        expires_at=clock + timedelta(seconds=ttl_seconds),
        next_cursor=None,
        seen_cursors=(),
        pages_completed=0,
        attempts_used=0,
        markets=(),
        seen_event_ids=frozenset(),
        seen_market_ids=frozenset(),
        seen_condition_ids=frozenset(),
        seen_asset_ids=frozenset(),
        diagnostics=(),
        updated_at=clock,
    )


def market_to_normalized_dict(market: ValidatedMarket) -> dict[str, Any]:
    return {
        "event_id": market.event_id,
        "market_id": market.market_id,
        "condition_id": market.condition_id,
        "event_slug": market.event_slug,
        "question": market.question,
        "category_ids": list(market.category_ids),
        "mode": market.mode.value,
        "liquidity_usd": str(market.liquidity_usd),
        "volume_24h_usd": str(market.volume_24h_usd),
        "metadata_at": market.metadata_at.astimezone(timezone.utc).isoformat(),
        "end_at": market.end_at.astimezone(timezone.utc).isoformat(),
        "outcomes": [
            {
                "asset_id": outcome.asset_id,
                "name": outcome.name,
                "price_hint": (
                    None if outcome.price_hint is None else str(outcome.price_hint)
                ),
                "index": outcome.index,
            }
            for outcome in market.outcomes
        ],
    }


def market_from_normalized_dict(payload: dict[str, Any]) -> ValidatedMarket:
    outcomes = tuple(
        ValidatedOutcome(
            asset_id=str(item["asset_id"]),
            name=str(item["name"]),
            price_hint=(
                None
                if item.get("price_hint") is None
                else Decimal(str(item["price_hint"]))
            ),
            index=int(item["index"]),
        )
        for item in payload["outcomes"]
    )
    return ValidatedMarket(
        event_id=str(payload["event_id"]),
        market_id=str(payload["market_id"]),
        condition_id=str(payload["condition_id"]),
        event_slug=str(payload["event_slug"]),
        question=str(payload["question"]),
        category_ids=tuple(str(value) for value in payload["category_ids"]),
        mode=ObservationMode(payload["mode"]),
        liquidity_usd=Decimal(str(payload["liquidity_usd"])),
        volume_24h_usd=Decimal(str(payload["volume_24h_usd"])),
        metadata_at=datetime.fromisoformat(str(payload["metadata_at"])),
        end_at=datetime.fromisoformat(str(payload["end_at"])),
        outcomes=outcomes,
    )


def session_to_normalized_dict(session: DiscoverySession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "version": session.version,
        "state": session.state.value,
        "started_at": session.started_at.astimezone(timezone.utc).isoformat(),
        "expires_at": session.expires_at.astimezone(timezone.utc).isoformat(),
        "next_cursor": session.next_cursor,
        "seen_cursors": list(session.seen_cursors),
        "pages_completed": session.pages_completed,
        "attempts_used": session.attempts_used,
        "markets": [market_to_normalized_dict(market) for market in session.markets],
        "seen_event_ids": sorted(session.seen_event_ids),
        "seen_market_ids": sorted(session.seen_market_ids),
        "seen_condition_ids": sorted(session.seen_condition_ids),
        "seen_asset_ids": sorted(session.seen_asset_ids),
        "diagnostics": [
            {
                "code": diagnostic.code.value,
                "event_id": diagnostic.event_id,
                "market_id": diagnostic.market_id,
            }
            for diagnostic in session.diagnostics
        ],
        "updated_at": session.updated_at.astimezone(timezone.utc).isoformat(),
    }


def session_from_normalized_dict(payload: dict[str, Any]) -> DiscoverySession:
    return DiscoverySession(
        session_id=str(payload["session_id"]),
        version=int(payload["version"]),
        state=ResultStatus(payload["state"]),
        started_at=datetime.fromisoformat(str(payload["started_at"])),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        next_cursor=payload.get("next_cursor"),
        seen_cursors=tuple(str(value) for value in payload.get("seen_cursors") or ()),
        pages_completed=int(payload["pages_completed"]),
        attempts_used=int(payload["attempts_used"]),
        markets=tuple(
            market_from_normalized_dict(item) for item in payload.get("markets") or ()
        ),
        seen_event_ids=frozenset(str(value) for value in payload.get("seen_event_ids") or ()),
        seen_market_ids=frozenset(
            str(value) for value in payload.get("seen_market_ids") or ()
        ),
        seen_condition_ids=frozenset(
            str(value) for value in payload.get("seen_condition_ids") or ()
        ),
        seen_asset_ids=frozenset(
            str(value) for value in payload.get("seen_asset_ids") or ()
        ),
        diagnostics=tuple(
            DiscoveryDiagnostic(
                code=OpportunityDiagnosticCode(item["code"]),
                event_id=item.get("event_id"),
                market_id=item.get("market_id"),
            )
            for item in payload.get("diagnostics") or ()
        ),
        updated_at=datetime.fromisoformat(str(payload["updated_at"])),
    )


def _record_diagnostics(
    diagnostics: list[DiscoveryDiagnostic],
    new_items: Iterable[DiscoveryDiagnostic],
) -> None:
    """Append informational diagnostics up to the bounded session limit."""
    for item in new_items:
        if len(diagnostics) >= MAX_SESSION_DIAGNOSTICS:
            return
        diagnostics.append(item)


def _failure_result(
    status: ResultStatus,
    code: OpportunityDiagnosticCode,
    *,
    pages_fetched: int,
    attempts_this_cycle: int,
    attempts_cumulative: int,
    pages_this_cycle: int,
    pages_cumulative: int,
    resumed: bool,
    session_id: str | None,
) -> DiscoveryResult:
    return DiscoveryResult(
        status=status,
        markets=(),
        diagnostics=(DiscoveryDiagnostic(code=code),),
        pages_fetched=pages_fetched,
        completed_at=None,
        gamma_attempts_this_cycle=attempts_this_cycle,
        gamma_attempts_cumulative=attempts_cumulative,
        pages_this_cycle=pages_this_cycle,
        pages_cumulative=pages_cumulative,
        session_resumed=resumed,
        session_id=session_id,
        session_state=status,
    )


def advance_discovery_session(
    client: GammaDiscoveryClient,
    session: DiscoverySession | None,
    *,
    now: datetime | None = None,
    limit: int = DISCOVERY_PAGE_LIMIT,
    max_attempts: int = LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
    ttl_seconds: int = DEFAULT_DISCOVERY_SESSION_TTL_SECONDS,
) -> DiscoveryAdvanceResult:
    """Consume at most ``max_attempts`` Gamma GETs and resume or complete."""
    if not 1 <= limit <= DISCOVERY_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {DISCOVERY_PAGE_LIMIT}")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    clock = _aware_utc(now or datetime.now(timezone.utc), "now")

    resumed = False
    if session is None:
        working = new_discovery_session(now=clock, ttl_seconds=ttl_seconds)
    else:
        if session.version != DISCOVERY_SESSION_VERSION:
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.DISCOVERY_SESSION_INVALID,
                    pages_fetched=0,
                    attempts_this_cycle=0,
                    attempts_cumulative=session.attempts_used,
                    pages_this_cycle=0,
                    pages_cumulative=session.pages_completed,
                    resumed=False,
                    session_id=session.session_id,
                ),
                session=None,
                resumed=False,
                pages_this_cycle=0,
                attempts_this_cycle=0,
                published_complete=False,
            )
        if session.is_expired(now=clock):
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.EXPIRED,
                    OpportunityDiagnosticCode.DISCOVERY_SESSION_EXPIRED,
                    pages_fetched=session.pages_completed,
                    attempts_this_cycle=0,
                    attempts_cumulative=session.attempts_used,
                    pages_this_cycle=0,
                    pages_cumulative=session.pages_completed,
                    resumed=False,
                    session_id=session.session_id,
                ),
                session=None,
                resumed=False,
                pages_this_cycle=0,
                attempts_this_cycle=0,
                published_complete=False,
            )
        if session.state not in {
            ResultStatus.NOT_STARTED,
            ResultStatus.IN_PROGRESS,
            ResultStatus.RETRYABLE_FAILURE,
        }:
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.DISCOVERY_SESSION_INVALID,
                    pages_fetched=session.pages_completed,
                    attempts_this_cycle=0,
                    attempts_cumulative=session.attempts_used,
                    pages_this_cycle=0,
                    pages_cumulative=session.pages_completed,
                    resumed=False,
                    session_id=session.session_id,
                ),
                session=None,
                resumed=False,
                pages_this_cycle=0,
                attempts_this_cycle=0,
                published_complete=False,
            )
        working = session
        resumed = session.pages_completed > 0 or session.next_cursor is not None

    cursor = working.next_cursor
    seen_cursors = set(working.seen_cursors)
    markets = list(working.markets)
    diagnostics = list(working.diagnostics)
    seen_events = set(working.seen_event_ids)
    seen_markets = set(working.seen_market_ids)
    seen_conditions = set(working.seen_condition_ids)
    seen_assets = set(working.seen_asset_ids)
    pages_completed = working.pages_completed
    attempts_used = working.attempts_used
    pages_this_cycle = 0
    attempts_this_cycle = 0
    terminated_normally = False

    while attempts_this_cycle < max_attempts:
        params: dict[str, Any] = {"closed": "false", "limit": limit}
        if cursor is not None:
            params["after_cursor"] = cursor
        try:
            payload = client.get_json("/events/keyset", params=params)
        except RequestBudgetExhausted:
            failed = replace(
                working,
                state=ResultStatus.PERMANENT_FAILURE,
                attempts_used=attempts_used + attempts_this_cycle,
                pages_completed=pages_completed,
                updated_at=clock,
                diagnostics=tuple(diagnostics)
                + (
                    DiscoveryDiagnostic(
                        code=OpportunityDiagnosticCode.GAMMA_BUDGET_EXHAUSTED
                    ),
                ),
            )
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.GAMMA_BUDGET_EXHAUSTED,
                    pages_fetched=pages_completed,
                    attempts_this_cycle=attempts_this_cycle,
                    attempts_cumulative=failed.attempts_used,
                    pages_this_cycle=pages_this_cycle,
                    pages_cumulative=pages_completed,
                    resumed=resumed,
                    session_id=working.session_id,
                ),
                session=None,
                resumed=resumed,
                pages_this_cycle=pages_this_cycle,
                attempts_this_cycle=attempts_this_cycle,
                published_complete=False,
            )
        except Exception as exc:
            status = _request_failure_status(exc)
            attempts_this_cycle += 1
            attempts_used += 1
            code = OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED
            if status == ResultStatus.RETRYABLE_FAILURE:
                preserved = DiscoverySession(
                    session_id=working.session_id,
                    version=working.version,
                    state=ResultStatus.RETRYABLE_FAILURE,
                    started_at=working.started_at,
                    expires_at=working.expires_at,
                    next_cursor=cursor,
                    seen_cursors=tuple(sorted(seen_cursors)),
                    pages_completed=pages_completed,
                    attempts_used=attempts_used,
                    markets=tuple(markets),
                    seen_event_ids=frozenset(seen_events),
                    seen_market_ids=frozenset(seen_markets),
                    seen_condition_ids=frozenset(seen_conditions),
                    seen_asset_ids=frozenset(seen_assets),
                    diagnostics=tuple(diagnostics)
                    + (DiscoveryDiagnostic(code=code),),
                    updated_at=clock,
                )
                return DiscoveryAdvanceResult(
                    result=_failure_result(
                        ResultStatus.RETRYABLE_FAILURE,
                        code,
                        pages_fetched=pages_completed,
                        attempts_this_cycle=attempts_this_cycle,
                        attempts_cumulative=attempts_used,
                        pages_this_cycle=pages_this_cycle,
                        pages_cumulative=pages_completed,
                        resumed=resumed,
                        session_id=working.session_id,
                    ),
                    session=preserved,
                    resumed=resumed,
                    pages_this_cycle=pages_this_cycle,
                    attempts_this_cycle=attempts_this_cycle,
                    published_complete=False,
                )
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.PERMANENT_FAILURE,
                    code,
                    pages_fetched=pages_completed,
                    attempts_this_cycle=attempts_this_cycle,
                    attempts_cumulative=attempts_used,
                    pages_this_cycle=pages_this_cycle,
                    pages_cumulative=pages_completed,
                    resumed=resumed,
                    session_id=working.session_id,
                ),
                session=None,
                resumed=resumed,
                pages_this_cycle=pages_this_cycle,
                attempts_this_cycle=attempts_this_cycle,
                published_complete=False,
            )

        attempts_this_cycle += 1
        attempts_used += 1
        pages_this_cycle += 1
        pages_completed += 1

        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID,
                    pages_fetched=pages_completed,
                    attempts_this_cycle=attempts_this_cycle,
                    attempts_cumulative=attempts_used,
                    pages_this_cycle=pages_this_cycle,
                    pages_cumulative=pages_completed,
                    resumed=resumed,
                    session_id=working.session_id,
                ),
                session=None,
                resumed=resumed,
                pages_this_cycle=pages_this_cycle,
                attempts_this_cycle=attempts_this_cycle,
                published_complete=False,
            )
        events = payload["events"]
        if any(not isinstance(event, dict) for event in events):
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID,
                    pages_fetched=pages_completed,
                    attempts_this_cycle=attempts_this_cycle,
                    attempts_cumulative=attempts_used,
                    pages_this_cycle=pages_this_cycle,
                    pages_cumulative=pages_completed,
                    resumed=resumed,
                    session_id=working.session_id,
                ),
                session=None,
                resumed=resumed,
                pages_this_cycle=pages_this_cycle,
                attempts_this_cycle=attempts_this_cycle,
                published_complete=False,
            )

        for raw_event in events:
            raw_event_id = raw_event.get("id")
            event_id = _validated_identifier(raw_event_id)
            if event_id is None:
                _record_diagnostics(
                    diagnostics,
                    (
                        DiscoveryDiagnostic(
                            code=OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID
                        ),
                    ),
                )
                continue
            if event_id in seen_events:
                return DiscoveryAdvanceResult(
                    result=_failure_result(
                        ResultStatus.PERMANENT_FAILURE,
                        OpportunityDiagnosticCode.DUPLICATE_EVENT,
                        pages_fetched=pages_completed,
                        attempts_this_cycle=attempts_this_cycle,
                        attempts_cumulative=attempts_used,
                        pages_this_cycle=pages_this_cycle,
                        pages_cumulative=pages_completed,
                        resumed=resumed,
                        session_id=working.session_id,
                    ),
                    session=None,
                    resumed=resumed,
                    pages_this_cycle=pages_this_cycle,
                    attempts_this_cycle=attempts_this_cycle,
                    published_complete=False,
                )
            seen_events.add(event_id)
            event_markets, event_diagnostics = _validate_event(
                raw_event, event_id, now=clock
            )
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
                return DiscoveryAdvanceResult(
                    result=_failure_result(
                        ResultStatus.PERMANENT_FAILURE,
                        fatal.code,
                        pages_fetched=pages_completed,
                        attempts_this_cycle=attempts_this_cycle,
                        attempts_cumulative=attempts_used,
                        pages_this_cycle=pages_this_cycle,
                        pages_cumulative=pages_completed,
                        resumed=resumed,
                        session_id=working.session_id,
                    ),
                    session=None,
                    resumed=resumed,
                    pages_this_cycle=pages_this_cycle,
                    attempts_this_cycle=attempts_this_cycle,
                    published_complete=False,
                )
            _record_diagnostics(diagnostics, event_diagnostics)
            for market in event_markets:
                if (
                    market.market_id in seen_markets
                    or market.condition_id in seen_conditions
                ):
                    return DiscoveryAdvanceResult(
                        result=_failure_result(
                            ResultStatus.PERMANENT_FAILURE,
                            OpportunityDiagnosticCode.DUPLICATE_MARKET,
                            pages_fetched=pages_completed,
                            attempts_this_cycle=attempts_this_cycle,
                            attempts_cumulative=attempts_used,
                            pages_this_cycle=pages_this_cycle,
                            pages_cumulative=pages_completed,
                            resumed=resumed,
                            session_id=working.session_id,
                        ),
                        session=None,
                        resumed=resumed,
                        pages_this_cycle=pages_this_cycle,
                        attempts_this_cycle=attempts_this_cycle,
                        published_complete=False,
                    )
                asset_ids = [outcome.asset_id for outcome in market.outcomes]
                if len(set(asset_ids)) != len(asset_ids) or any(
                    asset_id in seen_assets for asset_id in asset_ids
                ):
                    return DiscoveryAdvanceResult(
                        result=_failure_result(
                            ResultStatus.PERMANENT_FAILURE,
                            OpportunityDiagnosticCode.DUPLICATE_ASSET,
                            pages_fetched=pages_completed,
                            attempts_this_cycle=attempts_this_cycle,
                            attempts_cumulative=attempts_used,
                            pages_this_cycle=pages_this_cycle,
                            pages_cumulative=pages_completed,
                            resumed=resumed,
                            session_id=working.session_id,
                        ),
                        session=None,
                        resumed=resumed,
                        pages_this_cycle=pages_this_cycle,
                        attempts_this_cycle=attempts_this_cycle,
                        published_complete=False,
                    )
                seen_markets.add(market.market_id)
                seen_conditions.add(market.condition_id)
                seen_assets.update(asset_ids)
                markets.append(market)

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
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.DISCOVERY_CURSOR_INVALID,
                    pages_fetched=pages_completed,
                    attempts_this_cycle=attempts_this_cycle,
                    attempts_cumulative=attempts_used,
                    pages_this_cycle=pages_this_cycle,
                    pages_cumulative=pages_completed,
                    resumed=resumed,
                    session_id=working.session_id,
                ),
                session=None,
                resumed=resumed,
                pages_this_cycle=pages_this_cycle,
                attempts_this_cycle=attempts_this_cycle,
                published_complete=False,
            )
        if next_cursor in seen_cursors:
            return DiscoveryAdvanceResult(
                result=_failure_result(
                    ResultStatus.PERMANENT_FAILURE,
                    OpportunityDiagnosticCode.DISCOVERY_CURSOR_REPEAT,
                    pages_fetched=pages_completed,
                    attempts_this_cycle=attempts_this_cycle,
                    attempts_cumulative=attempts_used,
                    pages_this_cycle=pages_this_cycle,
                    pages_cumulative=pages_completed,
                    resumed=resumed,
                    session_id=working.session_id,
                ),
                session=None,
                resumed=resumed,
                pages_this_cycle=pages_this_cycle,
                attempts_this_cycle=attempts_this_cycle,
                published_complete=False,
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    if terminated_normally:
        markets.sort(key=lambda market: (market.event_id, market.market_id))
        complete = DiscoveryResult(
            status=ResultStatus.COMPLETE,
            markets=tuple(markets),
            diagnostics=tuple(diagnostics),
            pages_fetched=pages_completed,
            completed_at=clock,
            gamma_attempts_this_cycle=attempts_this_cycle,
            gamma_attempts_cumulative=attempts_used,
            pages_this_cycle=pages_this_cycle,
            pages_cumulative=pages_completed,
            session_resumed=resumed,
            session_id=working.session_id,
            session_state=ResultStatus.COMPLETE,
        )
        return DiscoveryAdvanceResult(
            result=complete,
            session=None,
            resumed=resumed,
            pages_this_cycle=pages_this_cycle,
            attempts_this_cycle=attempts_this_cycle,
            published_complete=True,
        )

    # Page/attempt budget boundary: keep session, publish no trusted universe.
    in_progress = DiscoverySession(
        session_id=working.session_id,
        version=working.version,
        state=ResultStatus.IN_PROGRESS,
        started_at=working.started_at,
        expires_at=working.expires_at,
        next_cursor=cursor,
        seen_cursors=tuple(sorted(seen_cursors)),
        pages_completed=pages_completed,
        attempts_used=attempts_used,
        markets=tuple(markets),
        seen_event_ids=frozenset(seen_events),
        seen_market_ids=frozenset(seen_markets),
        seen_condition_ids=frozenset(seen_conditions),
        seen_asset_ids=frozenset(seen_assets),
        diagnostics=tuple(diagnostics)
        + (DiscoveryDiagnostic(code=OpportunityDiagnosticCode.DISCOVERY_PAGE_CAP),),
        updated_at=clock,
    )
    return DiscoveryAdvanceResult(
        result=DiscoveryResult(
            status=ResultStatus.IN_PROGRESS,
            markets=(),
            diagnostics=(
                DiscoveryDiagnostic(code=OpportunityDiagnosticCode.DISCOVERY_PAGE_CAP),
            ),
            pages_fetched=pages_completed,
            completed_at=None,
            gamma_attempts_this_cycle=attempts_this_cycle,
            gamma_attempts_cumulative=attempts_used,
            pages_this_cycle=pages_this_cycle,
            pages_cumulative=pages_completed,
            session_resumed=resumed,
            session_id=working.session_id,
            session_state=ResultStatus.IN_PROGRESS,
        ),
        session=in_progress,
        resumed=resumed,
        pages_this_cycle=pages_this_cycle,
        attempts_this_cycle=attempts_this_cycle,
        published_complete=False,
    )
