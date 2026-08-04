"""Persisted trusted catalog publish/reload and stale-while-refresh proofs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database import opportunity_storage
from app.database.storage import initialize_database
from app.opportunity_intel.discovery import (
    CompleteDiscoveryCache,
    DiscoveryResult,
    ResultStatus,
    ValidatedMarket,
    ValidatedOutcome,
)
from app.opportunity_intel.models import ObservationMode
from tests.conftest import make_test_settings


def _market(market_id: str = "m1") -> ValidatedMarket:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    return ValidatedMarket(
        event_id="e1",
        market_id=market_id,
        condition_id="c1",
        event_slug="slug-1",
        question="Will it rain?",
        category_ids=("weather",),
        mode=ObservationMode.NORMAL,
        liquidity_usd=Decimal("5000"),
        volume_24h_usd=Decimal("12000"),
        metadata_at=now,
        end_at=now + timedelta(days=2),
        outcomes=(
            ValidatedOutcome(
                asset_id=f"a-{market_id}",
                name="Yes",
                price_hint=Decimal("0.42"),
                index=0,
            ),
        ),
    )


def test_publish_atomic_and_reload(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "cat.db"))
    initialize_database(settings)
    completed = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    version_id = opportunity_storage.publish_complete_catalog(
        settings,
        markets=[_market()],
        completed_at=completed,
        discovery_session_id="sess-1",
        pages_completed=12,
        diagnostics=("ok",),
    )
    loaded = opportunity_storage.load_active_catalog(settings)
    assert loaded is not None
    meta, markets = loaded
    assert meta.catalog_version_id == version_id
    assert meta.status == "ACTIVE"
    assert meta.market_count == 1
    assert len(markets) == 1
    assert markets[0].market_id == "m1"


def test_failed_publish_preserves_prior(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "preserve.db"))
    initialize_database(settings)
    completed = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    first = opportunity_storage.publish_complete_catalog(
        settings,
        markets=[_market("m1")],
        completed_at=completed,
        discovery_session_id="sess-1",
        pages_completed=3,
    )
    prior = opportunity_storage.load_active_catalog_version(settings)
    assert prior is not None
    assert prior.catalog_version_id == first

    class Boom:
        def __iter__(self):
            raise RuntimeError("inject publish failure")

    try:
        opportunity_storage.publish_complete_catalog(
            settings,
            markets=Boom(),
            completed_at=completed + timedelta(hours=1),
            discovery_session_id="sess-2",
            pages_completed=4,
        )
    except RuntimeError:
        pass

    after = opportunity_storage.load_active_catalog_version(settings)
    assert after is not None
    assert after.catalog_version_id == first
    markets = opportunity_storage.load_active_catalog_markets(settings)
    assert markets[0].market_id == "m1"


def test_retention_keeps_bounded_versions(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "ret.db"))
    initialize_database(settings)
    base = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    ids = []
    for idx in range(5):
        ids.append(
            opportunity_storage.publish_complete_catalog(
                settings,
                markets=[_market(f"m{idx}")],
                completed_at=base + timedelta(minutes=idx),
                discovery_session_id=f"sess-{idx}",
                pages_completed=idx + 1,
                retention_versions=3,
            )
        )
    with opportunity_storage._opportunity_connection(settings) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM opportunity_catalog_versions"
        ).fetchone()[0]
    assert int(count) == 3
    active = opportunity_storage.load_active_catalog_version(settings)
    assert active is not None
    assert active.catalog_version_id == ids[-1]


def test_cache_reload_and_stale_while_refresh(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "cache.db"))
    initialize_database(settings)
    completed = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    opportunity_storage.publish_complete_catalog(
        settings,
        markets=[_market()],
        completed_at=completed,
        discovery_session_id="sess-1",
        pages_completed=5,
    )

    cache = CompleteDiscoveryCache(
        ttl_seconds=300,
        catalog_ttl_seconds=21600,
        session_ttl_seconds=21600,
        settings=settings,
        persist_catalog=True,
    )
    soft_stale = completed + timedelta(seconds=301)
    usable = cache.get(now=soft_stale)
    assert usable is not None
    assert usable.complete
    assert usable.from_cache
    assert cache.needs_soft_refresh(now=soft_stale)

    hard_expired = completed + timedelta(seconds=21601)
    assert cache.get(now=hard_expired) is None


def test_publish_via_cache_then_restart_reload(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "restart.db"))
    initialize_database(settings)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    cache = CompleteDiscoveryCache(
        ttl_seconds=300,
        catalog_ttl_seconds=21600,
        settings=settings,
        persist_catalog=True,
    )
    result = DiscoveryResult(
        status=ResultStatus.COMPLETE,
        markets=(_market(),),
        diagnostics=(),
        pages_fetched=2,
        completed_at=now,
        pages_cumulative=2,
        session_id="sess-x",
        session_state=ResultStatus.COMPLETE,
    )
    published = cache._publish_complete(result)
    assert published.complete

    restarted = CompleteDiscoveryCache(
        ttl_seconds=300,
        catalog_ttl_seconds=21600,
        settings=settings,
        persist_catalog=True,
    )
    loaded = restarted.get(now=now + timedelta(seconds=10))
    assert loaded is not None
    assert len(loaded.markets) == 1
