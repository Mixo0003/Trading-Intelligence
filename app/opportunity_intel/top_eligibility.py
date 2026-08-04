"""Live eligibility filter for Top Opportunities display only.

Signal History must not import or call this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

import requests

from app.database.opportunity_storage import PersistedObservationBundle
from app.opportunity_intel.discovery import (
    ValidatedMarket,
    validate_gamma_market_for_tradeability,
)
from app.opportunity_intel.models import (
    ObservationMode,
    OpportunityDiagnosticCode,
    OpportunitySignal,
    SignalStatus,
)
from app.opportunity_intel.settings import (
    BALANCED_MAXIMUM_BOOK_AGE_SECONDS,
    NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS,
    STRICT_LIVE_LATEST_HISTORY_MAXIMUM_AGE_SECONDS,
    STRICT_LIVE_MAXIMUM_BOOK_AGE_SECONDS,
)
from app.polymarket.links import build_validated_market_link

logger = logging.getLogger(__name__)

# Same bound already used by Top Opportunities cycle staleness in catalog.py.
TOP_DISPLAY_FRESHNESS_SECONDS = float(NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS) * 30


class GammaLookupClient(Protocol):
    def get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any: ...


@dataclass(frozen=True)
class TopEligibilityResult:
    eligible: bool
    reason: OpportunityDiagnosticCode | None = None


def _aware(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    if value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _age_seconds(clock: datetime, value: datetime | None) -> float | None:
    aware = _aware(value)
    if aware is None:
        return None
    return (clock - aware).total_seconds()


def _valid_quote(value: Decimal | None) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and Decimal("0") <= value <= Decimal("1")
    )


def _valid_depth(value: Decimal | None) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def _mode_history_max_age(mode: ObservationMode) -> float:
    if mode == ObservationMode.LIVE_STRICT:
        return float(STRICT_LIVE_LATEST_HISTORY_MAXIMUM_AGE_SECONDS)
    return float(NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS)


def _mode_book_max_age(mode: ObservationMode) -> float:
    if mode == ObservationMode.LIVE_STRICT:
        return float(STRICT_LIVE_MAXIMUM_BOOK_AGE_SECONDS)
    return float(BALANCED_MAXIMUM_BOOK_AGE_SECONDS)


def evaluate_local_top_eligibility(
    *,
    signal: OpportunitySignal,
    bundle: PersistedObservationBundle,
    catalog_asset_ids: frozenset[str],
    now: datetime,
) -> TopEligibilityResult:
    """Fail-closed local checks before any Gamma HTTP call."""
    clock = now.astimezone(timezone.utc)
    if signal.status != SignalStatus.ACTIVE:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.MARKET_STATUS_INVALID)

    signal_at = _aware(signal.signal_at)
    observed_at = _aware(signal.source_observed_at)
    if signal_at is None or observed_at is None:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.MARKET_TIME_INVALID)
    if signal_at > clock or observed_at > clock:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.MARKET_TIME_INVALID)

    expires_at = _aware(signal.expires_at)
    if expires_at is None or expires_at <= clock:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.MARKET_EXPIRED)

    signal_age = _age_seconds(clock, signal_at)
    observed_age = _age_seconds(clock, observed_at)
    if (
        signal_age is None
        or observed_age is None
        or signal_age > TOP_DISPLAY_FRESHNESS_SECONDS
        or observed_age > TOP_DISPLAY_FRESHNESS_SECONDS
    ):
        return TopEligibilityResult(False, OpportunityDiagnosticCode.HISTORY_LATEST_STALE)

    end_at = _aware(bundle.end_at)
    if end_at is None:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.MARKET_TIME_INVALID)
    if end_at <= clock:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.MARKET_EXPIRED)

    if signal.asset_id not in catalog_asset_ids:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.MARKET_UNSAFE)

    history_at = _aware(bundle.history_at)
    book_at = _aware(bundle.book_at)
    if history_at is None:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.HISTORY_MISSING)
    if book_at is None:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.BOOK_MISSING)

    history_age = _age_seconds(clock, history_at)
    book_age = _age_seconds(clock, book_at)
    if history_age is None or history_age < 0 or history_age > TOP_DISPLAY_FRESHNESS_SECONDS:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.HISTORY_STALE)
    if book_age is None or book_age < 0 or book_age > TOP_DISPLAY_FRESHNESS_SECONDS:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.BOOK_STALE)

    # Observation-internal freshness: history/book must have been fresh vs observed_at.
    history_lag = (observed_at - history_at).total_seconds()
    book_lag = (observed_at - book_at).total_seconds()
    if history_lag < 0 or history_lag > _mode_history_max_age(signal.mode):
        return TopEligibilityResult(False, OpportunityDiagnosticCode.HISTORY_STALE)
    if book_lag < 0 or book_lag > _mode_book_max_age(signal.mode):
        return TopEligibilityResult(False, OpportunityDiagnosticCode.BOOK_STALE)

    if not (
        _valid_quote(bundle.best_bid)
        and _valid_quote(bundle.best_ask)
        and _valid_quote(bundle.midpoint)
    ):
        return TopEligibilityResult(False, OpportunityDiagnosticCode.BOOK_INVALID)
    if bundle.best_bid > bundle.best_ask:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.BOOK_CROSSED)
    if not (
        _valid_depth(bundle.visible_ask_depth_usd)
        and _valid_depth(bundle.visible_bid_depth_usd)
    ):
        return TopEligibilityResult(False, OpportunityDiagnosticCode.INSUFFICIENT_DEPTH)

    if build_validated_market_link(bundle.event_slug) is None:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.LINK_SLUG_INVALID)

    return TopEligibilityResult(True, None)


def _provider_failure(exc: BaseException) -> OpportunityDiagnosticCode:
    if isinstance(exc, requests.exceptions.Timeout):
        return OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED
    if isinstance(exc, requests.exceptions.ConnectionError):
        return OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED
    if isinstance(exc, requests.exceptions.HTTPError):
        return OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED
    return OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED


def _first_dict(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return None


def _resolve_raw_event(
    client: GammaLookupClient,
    *,
    raw_market: dict[str, Any],
    event_id: str,
    event_slug: str,
) -> tuple[dict[str, Any] | None, OpportunityDiagnosticCode | None]:
    events = raw_market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return events[0], None
    try:
        payload = client.get_json("/events", params={"slug": event_slug})
    except Exception as exc:  # noqa: BLE001 - fail closed on any provider fault
        return None, _provider_failure(exc)
    raw_event = _first_dict(payload)
    if raw_event is None:
        return None, OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID
    resolved_id = raw_event.get("id")
    if resolved_id is not None and str(resolved_id) != str(event_id):
        return None, OpportunityDiagnosticCode.LIVE_DATA_INCONSISTENT
    return raw_event, None


def evaluate_live_top_eligibility(
    client: GammaLookupClient,
    *,
    signal: OpportunitySignal,
    bundle: PersistedObservationBundle,
    now: datetime,
) -> TopEligibilityResult:
    """Fetch one Gamma market and apply the canonical tradeability validator."""
    clock = now.astimezone(timezone.utc)
    try:
        payload = client.get_json(
            "/markets",
            params={"condition_ids": bundle.condition_id},
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on any provider fault
        return TopEligibilityResult(False, _provider_failure(exc))

    raw_market = _first_dict(payload)
    if raw_market is None:
        return TopEligibilityResult(
            False, OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID
        )

    raw_event, event_error = _resolve_raw_event(
        client,
        raw_market=raw_market,
        event_id=bundle.event_id,
        event_slug=bundle.event_slug,
    )
    if event_error is not None or raw_event is None:
        return TopEligibilityResult(
            False,
            event_error or OpportunityDiagnosticCode.DISCOVERY_RESPONSE_INVALID,
        )

    validated, diagnostic = validate_gamma_market_for_tradeability(
        raw_event,
        raw_market,
        now=clock,
    )
    if diagnostic is not None or validated is None:
        code = (
            diagnostic.code
            if diagnostic is not None
            else OpportunityDiagnosticCode.MARKET_STATUS_INVALID
        )
        return TopEligibilityResult(False, code)

    if validated.condition_id != bundle.condition_id:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.LIVE_DATA_INCONSISTENT)
    if validated.end_at <= clock:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.MARKET_EXPIRED)

    outcome_assets = {outcome.asset_id for outcome in validated.outcomes}
    if signal.asset_id not in outcome_assets:
        return TopEligibilityResult(False, OpportunityDiagnosticCode.OUTCOME_TOKEN_MISMATCH)

    return TopEligibilityResult(True, None)


def evaluate_top_opportunity_eligibility(
    client: GammaLookupClient,
    *,
    signal: OpportunitySignal,
    bundle: PersistedObservationBundle,
    catalog_asset_ids: frozenset[str],
    now: datetime,
) -> TopEligibilityResult:
    """Full Top Opportunities eligibility: local then live Gamma, fail closed."""
    local = evaluate_local_top_eligibility(
        signal=signal,
        bundle=bundle,
        catalog_asset_ids=catalog_asset_ids,
        now=now,
    )
    if not local.eligible:
        logger.info(
            "Top Opportunities local exclude uid=%s asset=%s reason=%s",
            signal.opportunity_uid,
            signal.asset_id,
            local.reason.value if local.reason else "unknown",
        )
        return local

    live = evaluate_live_top_eligibility(
        client,
        signal=signal,
        bundle=bundle,
        now=now,
    )
    if not live.eligible:
        logger.info(
            "Top Opportunities live exclude uid=%s asset=%s reason=%s",
            signal.opportunity_uid,
            signal.asset_id,
            live.reason.value if live.reason else "unknown",
        )
    return live


def active_catalog_asset_ids(markets: tuple[ValidatedMarket, ...]) -> frozenset[str]:
    return frozenset(
        outcome.asset_id for market in markets for outcome in market.outcomes
    )
