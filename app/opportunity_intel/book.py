"""Narrow Opportunity adapter over V2.2.2 public CLOB book validation."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import requests

from app.config.settings import Settings
from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.order_book import (
    CLOCK_SKEW_TOLERANCE_SECONDS,
    ValidatedOrderBook,
    parse_validated_order_book,
)
from app.http.retry import get_with_retry, is_retryable_status, request_with_retry
from app.opportunity_intel.discovery import ResultStatus
from app.opportunity_intel.models import ObservationMode, OpportunityDiagnosticCode
from app.opportunity_intel.request_budget import CycleRequestBudget, RequestBudgetExhausted
from app.opportunity_intel.settings import (
    BALANCED_MAXIMUM_BOOK_AGE_SECONDS,
    STRICT_LIVE_MAXIMUM_BOOK_AGE_SECONDS,
)

BOOK_TIMEOUT_SECONDS = 15
# CLOB clocks often lead local time by a few seconds. Copy Intel still
# rejects that at 2s; Live Edge / Opportunity only retry within this cap.
_FUTURE_TIMESTAMP_SKEW_CAP_SECONDS = 60.0

_FAILURE_CODE_MAP: dict[str, OpportunityDiagnosticCode] = {
    "request_error": OpportunityDiagnosticCode.BOOK_UNAVAILABLE,
    "http_error": OpportunityDiagnosticCode.BOOK_UNAVAILABLE,
    "invalid_json": OpportunityDiagnosticCode.BOOK_UNAVAILABLE,
    "asset_mismatch": OpportunityDiagnosticCode.BOOK_ASSET_MISMATCH,
    "missing_asset_id": OpportunityDiagnosticCode.BOOK_ASSET_MISMATCH,
    "stale_quote": OpportunityDiagnosticCode.BOOK_STALE,
    "future_timestamp": OpportunityDiagnosticCode.BOOK_STALE,
    "ambiguous_timestamp": OpportunityDiagnosticCode.BOOK_INVALID,
    "crossed_or_locked_book": OpportunityDiagnosticCode.BOOK_CROSSED,
    "empty_bids": OpportunityDiagnosticCode.BOOK_ONE_SIDED,
    "empty_asks": OpportunityDiagnosticCode.BOOK_ONE_SIDED,
    "empty_book": OpportunityDiagnosticCode.BOOK_EMPTY,
    "missing_bids": OpportunityDiagnosticCode.BOOK_EMPTY,
    "missing_asks": OpportunityDiagnosticCode.BOOK_EMPTY,
}


@dataclass(frozen=True)
class BookAdapterFailure:
    status: ResultStatus
    diagnostic_code: OpportunityDiagnosticCode
    request_count: int = 1
    source_code: str | None = None


@dataclass(frozen=True)
class BookAdapterSuccess:
    status: ResultStatus
    book: ValidatedOrderBook
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    absolute_spread: Decimal
    relative_spread: Decimal
    visible_bid_depth_usd: Decimal
    visible_ask_depth_usd: Decimal
    book_at: datetime
    quote_age_seconds: Decimal
    request_count: int = 1


BookAdapterResult = BookAdapterSuccess | BookAdapterFailure


def max_book_age_seconds(mode: ObservationMode) -> float:
    if mode == ObservationMode.NORMAL:
        return float(BALANCED_MAXIMUM_BOOK_AGE_SECONDS)
    if mode == ObservationMode.LIVE_STRICT:
        return float(STRICT_LIVE_MAXIMUM_BOOK_AGE_SECONDS)
    raise ValueError("unsupported observation mode")


def map_book_failure_code(code: str) -> OpportunityDiagnosticCode:
    """Deterministically map V2.2.2 validator codes to opportunity diagnostics."""
    mapped = _FAILURE_CODE_MAP.get(code)
    if mapped is not None:
        return mapped
    if code.startswith("empty_"):
        return OpportunityDiagnosticCode.BOOK_ONE_SIDED
    if code.startswith("missing_"):
        return OpportunityDiagnosticCode.BOOK_INVALID
    if code.startswith("invalid_"):
        return OpportunityDiagnosticCode.BOOK_INVALID
    return OpportunityDiagnosticCode.BOOK_INVALID


def adapt_validated_book(
    book: ValidatedOrderBook,
    *,
    requested_asset: str,
    mode: ObservationMode,
    request_count: int = 1,
) -> BookAdapterResult:
    """Translate one validated V2.2.2 snapshot into Opportunity Decimal metrics."""
    if book.asset_id != str(requested_asset).strip():
        return BookAdapterFailure(
            status=ResultStatus.PERMANENT_FAILURE,
            diagnostic_code=OpportunityDiagnosticCode.BOOK_ASSET_MISMATCH,
            request_count=request_count,
            source_code="asset_mismatch",
        )
    max_age = max_book_age_seconds(mode)
    if book.quote_age_seconds > max_age:
        return BookAdapterFailure(
            status=ResultStatus.RETRYABLE_FAILURE,
            diagnostic_code=OpportunityDiagnosticCode.BOOK_STALE,
            request_count=request_count,
            source_code="stale_quote",
        )
    best_bid = _decimal_price(book.best_bid)
    best_ask = _decimal_price(book.best_ask)
    midpoint = _decimal_price(book.midpoint)
    absolute_spread = best_ask - best_bid
    if midpoint <= 0:
        return BookAdapterFailure(
            status=ResultStatus.PERMANENT_FAILURE,
            diagnostic_code=OpportunityDiagnosticCode.BOOK_INVALID,
            request_count=request_count,
            source_code="invalid_midpoint",
        )
    relative_spread = absolute_spread / midpoint
    return BookAdapterSuccess(
        status=ResultStatus.COMPLETE,
        book=book,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        absolute_spread=absolute_spread,
        relative_spread=relative_spread,
        visible_bid_depth_usd=_decimal_nonnegative(book.visible_bid_depth_usd),
        visible_ask_depth_usd=_decimal_nonnegative(book.visible_ask_depth_usd),
        book_at=datetime.fromtimestamp(book.quote_at, tz=timezone.utc),
        quote_age_seconds=Decimal(str(book.quote_age_seconds)),
        request_count=request_count,
    )


def validate_public_book_payload(
    payload: Any,
    *,
    requested_asset: str,
    mode: ObservationMode,
    received_at: float,
    request_count: int = 1,
) -> BookAdapterResult:
    """Reuse ``parse_validated_order_book`` with mode-specific freshness."""
    if isinstance(payload, dict):
        bids = payload.get("bids")
        asks = payload.get("asks")
        if (
            isinstance(bids, list)
            and isinstance(asks, list)
            and len(bids) == 0
            and len(asks) == 0
        ):
            return BookAdapterFailure(
                status=ResultStatus.PERMANENT_FAILURE,
                diagnostic_code=OpportunityDiagnosticCode.BOOK_EMPTY,
                request_count=request_count,
                source_code="empty_book",
            )
    parsed = parse_validated_order_book(
        payload,
        requested_asset=requested_asset,
        received_at=received_at,
        request_count=request_count,
        max_quote_age_seconds=max_book_age_seconds(mode),
    )
    if isinstance(parsed, ApiFailure):
        diagnostic = map_book_failure_code(parsed.code)
        status = (
            ResultStatus.RETRYABLE_FAILURE
            if parsed.retryable or diagnostic == OpportunityDiagnosticCode.BOOK_STALE
            else ResultStatus.PERMANENT_FAILURE
        )
        if diagnostic == OpportunityDiagnosticCode.BOOK_UNAVAILABLE:
            status = ResultStatus.RETRYABLE_FAILURE
        return BookAdapterFailure(
            status=status,
            diagnostic_code=diagnostic,
            request_count=parsed.request_count,
            source_code=parsed.code,
        )
    assert isinstance(parsed, ApiSuccess)
    return adapt_validated_book(
        parsed.data,
        requested_asset=requested_asset,
        mode=mode,
        request_count=parsed.request_count,
    )


class PublicOrderBookClient:
    """No-auth client for official CLOB ``/book`` via the V2.2.2 validator."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        request_get: Callable[..., requests.Response] = get_with_retry,
        timeout_seconds: int = BOOK_TIMEOUT_SECONDS,
        request_budget: CycleRequestBudget | None = None,
    ) -> None:
        configured = (
            base_url
            if base_url is not None
            else settings.polymarket_clob_base_url if settings is not None else None
        )
        if not configured:
            raise ValueError("public CLOB base URL is required")
        self.base_url = str(configured).rstrip("/")
        self.request_get = request_get
        self.timeout_seconds = int(timeout_seconds)
        self.request_budget = request_budget
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

    def fetch(
        self,
        asset_id: str,
        *,
        mode: ObservationMode,
        received_at: float | None = None,
    ) -> BookAdapterResult:
        url = f"{self.base_url}/book"
        request_count = 1
        try:
            if self.request_budget is not None:

                def _before(_attempt: int) -> None:
                    assert self.request_budget is not None
                    if not self.request_budget.try_consume_clob(1):
                        raise RequestBudgetExhausted(kind="clob")

                response = request_with_retry(
                    "GET",
                    url,
                    params={"token_id": asset_id},
                    headers={
                        "User-Agent": "Trading Intelligence Bot",
                        "Accept": "application/json",
                    },
                    timeout=self.timeout_seconds,
                    before_attempt=_before,
                )
            else:
                response = self.request_get(
                    url,
                    params={"token_id": asset_id},
                    headers={
                        "User-Agent": "Trading Intelligence Bot",
                        "Accept": "application/json",
                    },
                    timeout=self.timeout_seconds,
                )
            request_count = max(1, int(getattr(response, "request_count", 1) or 1))
        except RequestBudgetExhausted:
            return BookAdapterFailure(
                status=ResultStatus.PERMANENT_FAILURE,
                diagnostic_code=OpportunityDiagnosticCode.CLOB_BUDGET_EXHAUSTED,
                request_count=request_count,
                source_code="budget_exhausted",
            )
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.RequestException,
        ):
            return BookAdapterFailure(
                status=ResultStatus.RETRYABLE_FAILURE,
                diagnostic_code=OpportunityDiagnosticCode.BOOK_UNAVAILABLE,
                request_count=request_count,
                source_code="request_error",
            )
        received = float(received_at if received_at is not None else time.time())
        status_code = int(response.status_code)
        if status_code != 200:
            return BookAdapterFailure(
                status=(
                    ResultStatus.RETRYABLE_FAILURE
                    if is_retryable_status(status_code)
                    else ResultStatus.PERMANENT_FAILURE
                ),
                diagnostic_code=OpportunityDiagnosticCode.BOOK_UNAVAILABLE,
                request_count=request_count,
                source_code="http_error",
            )
        try:
            payload = response.json()
        except ValueError:
            return BookAdapterFailure(
                status=ResultStatus.PERMANENT_FAILURE,
                diagnostic_code=OpportunityDiagnosticCode.BOOK_UNAVAILABLE,
                request_count=request_count,
                source_code="invalid_json",
            )
        result = validate_public_book_payload(
            payload,
            requested_asset=asset_id,
            mode=mode,
            received_at=received,
            request_count=request_count,
        )
        if (
            isinstance(result, BookAdapterFailure)
            and result.source_code == "future_timestamp"
        ):
            retry_received = _received_at_for_future_book(payload, received)
            if retry_received is not None:
                result = validate_public_book_payload(
                    payload,
                    requested_asset=asset_id,
                    mode=mode,
                    received_at=retry_received,
                    request_count=request_count,
                )
        return result


def _received_at_for_future_book(payload: Any, received: float) -> float | None:
    """Shift receive-time only when CLOB timestamp is slightly ahead of local clock."""
    if not isinstance(payload, dict):
        return None
    try:
        book_ts = float(payload.get("timestamp"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(book_ts) or book_ts <= 0:
        return None
    candidate = book_ts / 1000.0 if book_ts > 1e12 else book_ts
    skew = candidate - received
    if skew <= 0 or skew > _FUTURE_TIMESTAMP_SKEW_CAP_SECONDS:
        return None
    return received + skew + CLOCK_SKEW_TOLERANCE_SECONDS


def _decimal_price(value: float) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number > 1:
        raise ValueError("book price out of bounds")
    return number


def _decimal_nonnegative(value: float) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("book depth must be non-negative")
    return number
