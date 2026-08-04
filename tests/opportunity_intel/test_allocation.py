"""V2.2.3 Checkpoint 3 — allocation sweeps reuse V2.2.2 allocate_buy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.copy_intel.api_result import ApiSuccess
from app.copy_intel.order_book import allocate_buy, parse_validated_order_book
from app.opportunity_intel.allocation import (
    CANONICAL_ALLOCATION_USD,
    sweep_allocation_presets,
)
from app.opportunity_intel.settings import ALLOCATION_PRESETS_USD

ASSET = "token-alloc-1"


def _validated_book(*, ask_size: str = "5000", ask_price: str = "0.50"):
    payload = {
        "asset_id": ASSET,
        "timestamp": 990.0,
        "hash": "hash-alloc",
        "bids": [{"price": "0.48", "size": "200"}],
        "asks": [{"price": ask_price, "size": ask_size}],
    }
    result = parse_validated_order_book(
        payload, requested_asset=ASSET, received_at=1000.0
    )
    assert isinstance(result, ApiSuccess)
    return result.data


def test_every_approved_preset_swept_on_same_snapshot():
    book = _validated_book()
    sweep = sweep_allocation_presets(book)
    assert len(sweep.metrics) == len(ALLOCATION_PRESETS_USD)
    assert tuple(metric.allocation_usd for metric in sweep.metrics) == ALLOCATION_PRESETS_USD
    assert all(metric.observed_at.timestamp() == pytest.approx(book.quote_at) for metric in sweep.metrics)
    assert sweep.book is book
    assert sweep.canonical.allocation_usd == CANONICAL_ALLOCATION_USD


def test_fully_executable_sweep_and_deterministic_vwap():
    book = _validated_book(ask_size="400", ask_price="0.50")
    # 100 USD / 0.50 = 200 shares exactly on one level.
    direct = allocate_buy(book, 100.0)
    sweep = sweep_allocation_presets(book)
    canonical = sweep.canonical
    assert direct.available is True
    assert canonical.fully_executable is True
    assert canonical.execution_vwap == Decimal(str(direct.execution_vwap))
    assert canonical.slippage == Decimal(str(direct.slippage))
    assert float(canonical.slippage) == pytest.approx(
        (float(canonical.execution_vwap) - book.midpoint) / book.midpoint
    )
    assert canonical.consumed_ask_usd == Decimal("100")


def test_exact_depth_boundary():
    # visible ask depth = 0.50 * 200 = 100 USD
    book = _validated_book(ask_size="200", ask_price="0.50")
    assert book.visible_ask_depth_usd == pytest.approx(100.0)
    sweep = sweep_allocation_presets(book)
    by_alloc = {metric.allocation_usd: metric for metric in sweep.metrics}
    assert by_alloc[Decimal("100")].fully_executable is True
    assert by_alloc[Decimal("250")].fully_executable is False
    assert by_alloc[Decimal("250")].execution_vwap is None
    assert by_alloc[Decimal("250")].slippage is None


def test_insufficient_depth_not_partially_executable():
    book = _validated_book(ask_size="50", ask_price="0.50")  # 25 USD depth
    sweep = sweep_allocation_presets(book)
    for metric in sweep.metrics:
        if metric.allocation_usd > Decimal("25"):
            assert metric.fully_executable is False
            assert metric.quantity == Decimal("0")
            assert metric.execution_vwap is None
            assert metric.slippage is None
        else:
            assert metric.fully_executable is True


def test_existing_v222_slippage_semantics_unchanged():
    book = _validated_book(
        ask_size="1000",
        ask_price="0.55",
    )
    # midpoint = (0.48 + 0.55) / 2 = 0.515; VWAP = 0.55
    exe = allocate_buy(book, 100.0)
    sweep = sweep_allocation_presets(book)
    assert exe.available
    assert sweep.canonical.fully_executable
    expected = (exe.execution_vwap - book.midpoint) / book.midpoint
    assert float(sweep.canonical.slippage) == pytest.approx(expected)
    assert float(sweep.canonical.execution_vwap) == pytest.approx(exe.execution_vwap)
