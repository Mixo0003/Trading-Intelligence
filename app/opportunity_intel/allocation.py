"""Allocation metrics for approved Opportunity presets via V2.2.2 allocate_buy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.copy_intel.order_book import AllocationExecution, ValidatedOrderBook, allocate_buy
from app.opportunity_intel.models import ExecutionMetrics, OpportunityDiagnosticCode
from app.opportunity_intel.settings import (
    ALLOCATION_PRESETS_USD,
    BALANCED_PAPER_ALLOCATION_USD,
)

CANONICAL_ALLOCATION_USD = BALANCED_PAPER_ALLOCATION_USD


@dataclass(frozen=True)
class AllocationSweepResult:
    book: ValidatedOrderBook
    metrics: tuple[ExecutionMetrics, ...]
    canonical: ExecutionMetrics

    @property
    def book_fingerprint(self) -> tuple[str, float, str]:
        return (self.book.asset_id, self.book.quote_at, self.book.book_hash)


def sweep_allocation_presets(
    book: ValidatedOrderBook,
    *,
    observed_at: datetime | None = None,
    presets: tuple[Decimal, ...] = ALLOCATION_PRESETS_USD,
) -> AllocationSweepResult:
    """Run ``allocate_buy`` once per approved preset against one snapshot."""
    if observed_at is None:
        book_at = datetime.fromtimestamp(book.quote_at, tz=timezone.utc)
    else:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        book_at = observed_at.astimezone(timezone.utc)

    metrics: list[ExecutionMetrics] = []
    for preset in presets:
        execution = allocate_buy(book, float(preset))
        metrics.append(
            execution_metrics_from_allocation(
                execution,
                asset_id=book.asset_id,
                observed_at=book_at,
                allocation_usd=preset,
            )
        )
    ordered = tuple(metrics)
    canonical = next(
        metric for metric in ordered if metric.allocation_usd == CANONICAL_ALLOCATION_USD
    )
    return AllocationSweepResult(book=book, metrics=ordered, canonical=canonical)


def execution_metrics_from_allocation(
    execution: AllocationExecution,
    *,
    asset_id: str,
    observed_at: datetime,
    allocation_usd: Decimal,
) -> ExecutionMetrics:
    """Map V2.2.2 AllocationExecution into Opportunity ExecutionMetrics."""
    if execution.available:
        if execution.execution_vwap is None or execution.slippage is None:
            raise ValueError("available allocation missing VWAP or slippage")
        if execution.filled_shares is None:
            raise ValueError("available allocation missing filled quantity")
        return ExecutionMetrics(
            asset_id=asset_id,
            observed_at=observed_at,
            allocation_usd=allocation_usd,
            fully_executable=True,
            execution_vwap=Decimal(str(execution.execution_vwap)),
            slippage=Decimal(str(execution.slippage)),
            quantity=Decimal(str(execution.filled_shares)),
            consumed_ask_usd=allocation_usd,
        )
    return ExecutionMetrics(
        asset_id=asset_id,
        observed_at=observed_at,
        allocation_usd=allocation_usd,
        fully_executable=False,
        execution_vwap=None,
        slippage=None,
        quantity=Decimal("0"),
        consumed_ask_usd=Decimal("0"),
    )


def insufficient_depth_diagnostic(
    execution: AllocationExecution,
) -> OpportunityDiagnosticCode | None:
    if execution.available:
        return None
    if execution.unavailable_code == "insufficient_depth":
        return OpportunityDiagnosticCode.INSUFFICIENT_DEPTH
    if execution.unavailable_code in {
        "partial_fill",
        "invalid_fill",
        "below_min_order_size",
        "invalid_allocation",
        "invalid_midpoint",
    }:
        return OpportunityDiagnosticCode.INSUFFICIENT_DEPTH
    return OpportunityDiagnosticCode.INSUFFICIENT_DEPTH
