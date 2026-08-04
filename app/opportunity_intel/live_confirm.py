"""STRICT live two-book confirmation with injectable delay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Protocol

from app.copy_intel.order_book import ValidatedOrderBook
from app.opportunity_intel.book import (
    BookAdapterFailure,
    BookAdapterSuccess,
    adapt_validated_book,
)
from app.opportunity_intel.models import ObservationMode, OpportunityDiagnosticCode
from app.opportunity_intel.versions import (
    STRICT_LIVE_CONFIRM_DELAY_MAX_SECONDS,
    STRICT_LIVE_CONFIRM_DELAY_MIN_SECONDS,
    STRICT_LIVE_CONFIRM_DELAY_SECONDS,
)


class DelayFn(Protocol):
    def __call__(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class LiveConfirmationSuccess:
    first: ValidatedOrderBook
    second: ValidatedOrderBook
    first_adapted: BookAdapterSuccess
    second_adapted: BookAdapterSuccess


@dataclass(frozen=True)
class LiveConfirmationFailure:
    diagnostic_code: OpportunityDiagnosticCode
    detail_code: str | None = None


LiveConfirmationResult = LiveConfirmationSuccess | LiveConfirmationFailure


def confirm_strict_live_books(
    *,
    asset_id: str,
    fetch_book: Callable[[], ValidatedOrderBook | BookAdapterFailure],
    delay: DelayFn,
    delay_seconds: float = STRICT_LIVE_CONFIRM_DELAY_SECONDS,
) -> LiveConfirmationResult:
    """Fetch two STRICT books separated by an injected delay; no real sleep required."""
    if not (
        STRICT_LIVE_CONFIRM_DELAY_MIN_SECONDS
        <= delay_seconds
        <= STRICT_LIVE_CONFIRM_DELAY_MAX_SECONDS
    ):
        raise ValueError("STRICT live confirmation delay out of approved bounds")

    first_raw = fetch_book()
    if isinstance(first_raw, BookAdapterFailure):
        return LiveConfirmationFailure(
            diagnostic_code=first_raw.diagnostic_code,
            detail_code=first_raw.source_code,
        )
    first = first_raw
    first_adapted = adapt_validated_book(
        first, requested_asset=asset_id, mode=ObservationMode.LIVE_STRICT
    )
    if isinstance(first_adapted, BookAdapterFailure):
        return LiveConfirmationFailure(
            diagnostic_code=first_adapted.diagnostic_code,
            detail_code=first_adapted.source_code,
        )

    delay(float(delay_seconds))

    second_raw = fetch_book()
    if isinstance(second_raw, BookAdapterFailure):
        return LiveConfirmationFailure(
            diagnostic_code=second_raw.diagnostic_code,
            detail_code=second_raw.source_code,
        )
    second = second_raw
    if second.asset_id != asset_id or first.asset_id != asset_id:
        return LiveConfirmationFailure(
            diagnostic_code=OpportunityDiagnosticCode.BOOK_ASSET_MISMATCH,
            detail_code="asset_mismatch",
        )
    second_adapted = adapt_validated_book(
        second, requested_asset=asset_id, mode=ObservationMode.LIVE_STRICT
    )
    if isinstance(second_adapted, BookAdapterFailure):
        return LiveConfirmationFailure(
            diagnostic_code=second_adapted.diagnostic_code,
            detail_code=second_adapted.source_code,
        )

    delta = abs(second_adapted.midpoint - first_adapted.midpoint)
    limit = max(Decimal("0.01"), second_adapted.absolute_spread)
    if delta > limit:
        return LiveConfirmationFailure(
            diagnostic_code=OpportunityDiagnosticCode.LIVE_BOOK_UNSTABLE,
            detail_code="inter_read_instability",
        )
    return LiveConfirmationSuccess(
        first=first,
        second=second,
        first_adapted=first_adapted,
        second_adapted=second_adapted,
    )


def noop_delay(_seconds: float) -> None:
    """Test-friendly delay that does not sleep."""
    return None
