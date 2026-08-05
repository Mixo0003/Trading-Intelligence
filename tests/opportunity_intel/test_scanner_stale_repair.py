"""Scanner stale/rediscovery repair: SWR evaluation, status, and cycle completion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.database.opportunity_storage import (
    SqliteDiscoverySessionStore,
    initialize_opportunity_schema,
    publish_complete_catalog,
    save_scanner_state,
)
from app.database.storage import initialize_database
from app.opportunity_intel.discovery import (
    CompleteDiscoveryCache,
    ResultStatus,
    ValidatedMarket,
    ValidatedOutcome,
)
from app.opportunity_intel.models import (
    ObservationMode,
    OpportunityScannerState,
    ScannerStatus,
)
from app.opportunity_intel.runtime import _finalize_successful_cycle
from app.opportunity_intel.scanner import ScannerCycleResult
from app.opportunity_intel.status_view import build_scanner_status_view
from tests.conftest import make_test_settings
from tests.opportunity_intel.test_discovery import _client, _event, _market

NOW = datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "stale_repair.db"),
        admin_ids=[1],
        opportunity_scanner_enabled=True,
        opportunity_auto_alerts_enabled=True,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    return value


def _validated_market() -> ValidatedMarket:
    return ValidatedMarket(
        event_id="e1",
        market_id="m1",
        condition_id="c1",
        event_slug="slug-1",
        question="Will it rain?",
        category_ids=("weather",),
        mode=ObservationMode.NORMAL,
        liquidity_usd=Decimal("5000"),
        volume_24h_usd=Decimal("12000"),
        metadata_at=NOW,
        end_at=NOW + timedelta(days=2),
        outcomes=(
            ValidatedOutcome(
                asset_id="a1",
                name="Yes",
                price_hint=Decimal("0.42"),
                index=0,
            ),
        ),
    )


def _scanner_state(
    *,
    status: ScannerStatus,
    last: datetime,
    discovery_complete: bool = True,
) -> OpportunityScannerState:
    return OpportunityScannerState(
        status=status,
        schema_version=1,
        baseline_version=1,
        score_version=1,
        global_baseline_cycles=2,
        discovery_complete=discovery_complete,
        last_complete_discovery_at=last,
        last_complete_cycle_at=last,
        diagnostic_code=None,
        updated_at=last,
    )


def test_zero_signal_cycle_still_finalizes_without_false_timeout(settings):
    cycle = ScannerCycleResult(
        cycle_id="c-zero",
        discovery_status=ResultStatus.COMPLETE,
        markets_discovered=1,
        outcomes_shortlisted=0,
        histories_valid=0,
        histories_failed=0,
        books_valid=0,
        books_failed=0,
        observations_eligible=0,
        observations_rejected=0,
        baseline_advanced=0,
        signals_created=0,
        signals_materially_updated=0,
        signals_expired=0,
        storage_ok=True,
        delivery_signal_uids=(),
    )
    result = _finalize_successful_cycle(
        settings,
        cycle=cycle,
        clock=NOW,
        deadline_token=None,
        bot_data={},
        suppress_selection=False,
    )
    assert result.timed_out is False
    assert result.selection_ran is False
    assert result.cycle is not None
    assert result.cycle.storage_ok is True


def test_status_discovering_when_progress_fresh_and_cycle_old(settings):
    completed = NOW - timedelta(hours=3)
    store = SqliteDiscoverySessionStore(settings)
    cache = CompleteDiscoveryCache(
        ttl_seconds=300,
        session_ttl_seconds=3600,
        session_store=store,
        settings=settings,
        persist_catalog=True,
    )
    client = _client({"events": [_event("e1")], "next_cursor": "cursor-a"})
    progressing = cache.advance_from_client(client, now=NOW, max_attempts=1)
    assert progressing.status == ResultStatus.IN_PROGRESS

    save_scanner_state(
        settings,
        _scanner_state(status=ScannerStatus.HEALTHY, last=completed),
    )
    view = build_scanner_status_view(settings, now=NOW + timedelta(seconds=30))
    assert view.discovery_label == "in progress"
    assert view.cache_age_seconds is not None and view.cache_age_seconds > 3600
    assert view.label == "Discovering"


def test_status_stale_when_progress_and_cycle_both_old(settings):
    from tests.opportunity_intel.test_telegram_ux import _seed_ready

    completed = NOW - timedelta(hours=3)
    _seed_ready(settings, score="85")
    store = SqliteDiscoverySessionStore(settings)
    cache = CompleteDiscoveryCache(
        ttl_seconds=300,
        session_ttl_seconds=3600,
        session_store=store,
        settings=settings,
        persist_catalog=True,
    )
    client = _client({"events": [_event("e1")], "next_cursor": "cursor-a"})
    cache.advance_from_client(client, now=completed, max_attempts=1)

    save_scanner_state(
        settings,
        _scanner_state(status=ScannerStatus.HEALTHY, last=completed),
    )
    view = build_scanner_status_view(settings, now=NOW)
    assert view.label == "Stale"


def test_advance_serves_expired_catalog_while_refresh_in_progress(settings):
    completed = NOW - timedelta(seconds=21601)
    publish_complete_catalog(
        settings,
        markets=[_validated_market()],
        completed_at=completed,
        discovery_session_id="sess-1",
        pages_completed=3,
    )
    store = SqliteDiscoverySessionStore(settings)
    cache = CompleteDiscoveryCache(
        ttl_seconds=300,
        catalog_ttl_seconds=21600,
        session_ttl_seconds=21600,
        session_store=store,
        settings=settings,
        persist_catalog=True,
    )
    assert cache.get(now=NOW) is None

    client = _client({"events": [_event("e2", markets=[_market("m2")])], "next_cursor": "c-next"})
    advanced = cache.advance_from_client(client, now=NOW, max_attempts=1)
    assert advanced.status == ResultStatus.COMPLETE
    assert advanced.refresh_failure_status == ResultStatus.IN_PROGRESS
    assert len(advanced.markets) == 1
    assert advanced.markets[0].market_id == "m1"


def test_hard_ttl_get_still_fail_closed(settings):
    completed = NOW - timedelta(seconds=21601)
    publish_complete_catalog(
        settings,
        markets=[_validated_market()],
        completed_at=completed,
        discovery_session_id="sess-1",
        pages_completed=3,
    )
    cache = CompleteDiscoveryCache(
        ttl_seconds=300,
        catalog_ttl_seconds=21600,
        settings=settings,
        persist_catalog=True,
    )
    assert cache.get(now=NOW) is None
    assert cache.get_stale_while_revalidate(now=NOW) is not None
