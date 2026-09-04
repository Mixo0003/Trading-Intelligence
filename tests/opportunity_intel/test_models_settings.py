from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, time, timezone
from decimal import Decimal

import pytest

from app.opportunity_intel.models import (
    AlertMode,
    LiveMode,
    OpportunityBaseline,
    ObservationMode,
)
from app.opportunity_intel.settings import (
    ALLOCATION_PRESETS_USD,
    BALANCED_COOLDOWN_SECONDS,
    BALANCED_MAXIMUM_ABSOLUTE_SPREAD,
    BALANCED_MAXIMUM_RELATIVE_SPREAD,
    BALANCED_MAXIMUM_SLIPPAGE,
    BALANCED_MAXIMUM_BOOK_AGE_SECONDS,
    BALANCED_MINIMUM_24H_VOLUME_USD,
    BALANCED_MINIMUM_LIQUIDITY_USD,
    BALANCED_MINIMUM_SCORE,
    BALANCED_MINIMUM_TIME_REMAINING_SECONDS,
    BALANCED_PAPER_ALLOCATION_USD,
    COOLDOWN_SECONDS_BOUNDS,
    MAXIMUM_ABSOLUTE_SPREAD_BOUNDS,
    MAXIMUM_RELATIVE_SPREAD_BOUNDS,
    MAXIMUM_SLIPPAGE_BOUNDS,
    MINIMUM_24H_VOLUME_USD_BOUNDS,
    MINIMUM_LIQUIDITY_USD_BOUNDS,
    MINIMUM_SCORE_BOUNDS,
    STRICT_LIVE_MAXIMUM_ABSOLUTE_SPREAD,
    STRICT_LIVE_MAXIMUM_BOOK_AGE_SECONDS,
    STRICT_LIVE_MAXIMUM_RELATIVE_SPREAD,
    STRICT_LIVE_MAXIMUM_SLIPPAGE,
    STRICT_LIVE_MINIMUM_24H_VOLUME_USD,
    STRICT_LIVE_MINIMUM_LIQUIDITY_USD,
    STRICT_LIVE_MINIMUM_SCORE,
    CategoryAllowlist,
    OpportunityScannerSettings,
    QuietHours,
    balanced_settings,
)


def test_balanced_defaults_are_exact_and_immutable():
    value = balanced_settings()
    assert value.revision == 0
    assert value.minimum_score == BALANCED_MINIMUM_SCORE == 65
    assert value.minimum_liquidity_usd == BALANCED_MINIMUM_LIQUIDITY_USD
    assert value.minimum_24h_volume_usd == BALANCED_MINIMUM_24H_VOLUME_USD
    assert value.maximum_relative_spread == BALANCED_MAXIMUM_RELATIVE_SPREAD
    assert value.maximum_absolute_spread == BALANCED_MAXIMUM_ABSOLUTE_SPREAD
    assert value.maximum_slippage == BALANCED_MAXIMUM_SLIPPAGE
    assert value.paper_allocation_usd == BALANCED_PAPER_ALLOCATION_USD
    assert value.live_mode == LiveMode.OFF
    assert value.live_edge_enabled is False
    assert value.alert_mode == AlertMode.ON_DEMAND_ONLY
    assert value.cooldown_seconds == BALANCED_COOLDOWN_SECONDS
    assert value.categories == CategoryAllowlist()
    assert value.timezone_name == "UTC"
    with pytest.raises(FrozenInstanceError):
        value.minimum_score = 70  # type: ignore[misc]
    assert BALANCED_MINIMUM_TIME_REMAINING_SECONDS == 21600
    assert BALANCED_MAXIMUM_BOOK_AGE_SECONDS == 30


def test_strict_live_policy_defaults_are_centralized():
    assert STRICT_LIVE_MINIMUM_SCORE == 75
    assert STRICT_LIVE_MINIMUM_LIQUIDITY_USD == Decimal("25000")
    assert STRICT_LIVE_MINIMUM_24H_VOLUME_USD == Decimal("50000")
    assert STRICT_LIVE_MAXIMUM_RELATIVE_SPREAD == Decimal("0.05")
    assert STRICT_LIVE_MAXIMUM_ABSOLUTE_SPREAD == Decimal("0.03")
    assert STRICT_LIVE_MAXIMUM_SLIPPAGE == Decimal("0.01")
    assert STRICT_LIVE_MAXIMUM_BOOK_AGE_SECONDS == 10


@pytest.mark.parametrize("minimum_score", MINIMUM_SCORE_BOUNDS)
def test_score_bounds_accept_endpoints(minimum_score):
    assert OpportunityScannerSettings(minimum_score=minimum_score).minimum_score == minimum_score


@pytest.mark.parametrize("minimum_score", [49, 96, 65.5])
def test_score_bounds_reject_outside_or_non_integer(minimum_score):
    with pytest.raises(ValueError):
        OpportunityScannerSettings(minimum_score=minimum_score)


@pytest.mark.parametrize(
    ("field_name", "bounds"),
    [
        ("minimum_liquidity_usd", MINIMUM_LIQUIDITY_USD_BOUNDS),
        ("minimum_24h_volume_usd", MINIMUM_24H_VOLUME_USD_BOUNDS),
        ("maximum_relative_spread", MAXIMUM_RELATIVE_SPREAD_BOUNDS),
        ("maximum_absolute_spread", MAXIMUM_ABSOLUTE_SPREAD_BOUNDS),
        ("maximum_slippage", MAXIMUM_SLIPPAGE_BOUNDS),
    ],
)
def test_decimal_bounds_accept_every_endpoint(field_name, bounds):
    for endpoint in bounds:
        value = OpportunityScannerSettings(**{field_name: endpoint})
        assert getattr(value, field_name) == endpoint


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("minimum_liquidity_usd", Decimal("4999.99")),
        ("minimum_liquidity_usd", Decimal("1000000.01")),
        ("minimum_24h_volume_usd", Decimal("9999.99")),
        ("minimum_24h_volume_usd", Decimal("10000000.01")),
        ("maximum_relative_spread", Decimal("0.009")),
        ("maximum_relative_spread", Decimal("0.151")),
        ("maximum_absolute_spread", Decimal("0.009")),
        ("maximum_absolute_spread", Decimal("0.081")),
        ("maximum_slippage", Decimal("0.004")),
        ("maximum_slippage", Decimal("0.051")),
    ],
)
def test_decimal_bounds_reject_outside(field_name, value):
    with pytest.raises(ValueError):
        OpportunityScannerSettings(**{field_name: value})


def test_decimal_fields_reject_binary_float():
    with pytest.raises(TypeError):
        OpportunityScannerSettings(minimum_liquidity_usd=10_000.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("allocation", ALLOCATION_PRESETS_USD)
def test_every_allocation_preset_is_valid(allocation):
    assert OpportunityScannerSettings(paper_allocation_usd=allocation).paper_allocation_usd == allocation


@pytest.mark.parametrize("allocation", [Decimal("0"), Decimal("75"), Decimal("100.01")])
def test_invalid_allocation_rejected(allocation):
    with pytest.raises(ValueError):
        OpportunityScannerSettings(paper_allocation_usd=allocation)


@pytest.mark.parametrize("cooldown", COOLDOWN_SECONDS_BOUNDS)
def test_cooldown_bounds_accept_endpoints(cooldown):
    assert OpportunityScannerSettings(cooldown_seconds=cooldown).cooldown_seconds == cooldown


@pytest.mark.parametrize("cooldown", [899, 86401])
def test_invalid_cooldown_rejected(cooldown):
    with pytest.raises(ValueError):
        OpportunityScannerSettings(cooldown_seconds=cooldown)


def test_live_and_alert_enums_are_typed():
    strict = OpportunityScannerSettings(
        live_mode=LiveMode.STRICT,
        alert_mode=AlertMode.INSTANT,
    )
    assert strict.live_mode == LiveMode.STRICT
    assert strict.alert_mode == AlertMode.INSTANT
    with pytest.raises(ValueError):
        OpportunityScannerSettings(live_mode="STRICT")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OpportunityScannerSettings(alert_mode="INSTANT")  # type: ignore[arg-type]


def test_category_allowlist_validation():
    custom = CategoryAllowlist(
        all_categories=False,
        category_ids=frozenset({"politics", "sports:nfl"}),
    )
    assert custom.category_ids == frozenset({"politics", "sports:nfl"})
    with pytest.raises(ValueError):
        CategoryAllowlist(all_categories=True, category_ids=frozenset({"politics"}))
    with pytest.raises(ValueError):
        CategoryAllowlist(all_categories=False, category_ids=frozenset())
    for malformed in ("bad category", "../sports", "x/y", "", "x" * 65):
        with pytest.raises(ValueError):
            CategoryAllowlist(
                all_categories=False,
                category_ids=frozenset({malformed}),
            )


def test_quiet_hours_and_timezone_validation():
    quiet = QuietHours(start=time(22, 0), end=time(7, 0))
    value = OpportunityScannerSettings(
        quiet_hours=quiet,
        timezone_name="America/Chicago",
    )
    assert value.quiet_hours == quiet
    with pytest.raises(ValueError):
        QuietHours(start=time(8, 0), end=time(8, 0))
    with pytest.raises(ValueError):
        OpportunityScannerSettings(timezone_name="Not/A_Real_Zone")
    with pytest.raises(ValueError):
        OpportunityScannerSettings(timezone_name=" UTC ")


def test_revision_is_explicit_and_non_negative():
    current = balanced_settings(revision=4)
    updated = replace(current, revision=5, minimum_score=70)
    assert current.revision == 4
    assert updated.revision == 5
    assert updated.minimum_score == 70
    with pytest.raises(ValueError):
        balanced_settings(revision=-1)


def test_baseline_requires_utc_aware_timestamp_and_consistent_readiness():
    now = datetime.now(timezone.utc)
    value = OpportunityBaseline(
        asset_id="asset-1",
        mode=ObservationMode.NORMAL,
        baseline_version=1,
        consecutive_complete_cycles=2,
        last_complete_cycle_id="cycle-2",
        last_observed_at=now,
        ready=True,
    )
    assert value.last_observed_at is not None
    assert value.last_observed_at.utcoffset() == timezone.utc.utcoffset(now)
    with pytest.raises(ValueError):
        replace(value, last_observed_at=datetime.now())
    with pytest.raises(ValueError):
        replace(value, consecutive_complete_cycles=1, ready=True)
