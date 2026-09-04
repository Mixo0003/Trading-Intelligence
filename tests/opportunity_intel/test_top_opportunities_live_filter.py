"""Top Opportunities live-only eligibility; Signal History stays historical."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests

import sqlite3

from app.database import opportunity_storage
from app.database.opportunity_storage import (
    initialize_opportunity_schema,
    list_recent_signals,
    load_market_state,
    save_signal,
)
from app.database.storage import initialize_database, upsert_user
from app.opportunity_intel.catalog import load_top_opportunities
from app.opportunity_intel.discovery import ValidatedMarket, ValidatedOutcome
from app.opportunity_intel.models import (
    MovementDirection,
    ObservationMode,
    OpportunityReasonCode,
    OpportunitySignal,
    SignalFamily,
    SignalStatus,
)
from app.opportunity_intel.top_eligibility import (
    TOP_DISPLAY_FRESHNESS_SECONDS,
    evaluate_local_top_eligibility,
)
from app.telegram.opportunity_intel.formatting import (
    format_opportunity_card,
    format_signal_history,
)
from tests.conftest import make_test_settings
from tests.opportunity_intel.test_telegram_ux import _live_gamma_client, _seed_ready

USER_ID = 42
NOW = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)


def _db(settings):
    return sqlite3.connect(settings.database_path)


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "top_live.db"),
        admin_ids=[1],
        opportunity_scanner_enabled=True,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    upsert_user(value, USER_ID, "user", "User")
    return value


def _load(settings, *, gamma_client=None, now=NOW):
    return load_top_opportunities(
        settings,
        telegram_id=USER_ID,
        now=now,
        gamma_client=gamma_client or _live_gamma_client(now=now),
    )


def _seed(settings, **kwargs):
    signal = _seed_ready(settings, **kwargs)
    connection = _db(settings)
    try:
        connection.execute(
            """
            UPDATE opportunity_metric_buckets
            SET observed_at = ?, bucket_at = ?, book_at = ?, history_at = ?
            """,
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            "UPDATE opportunity_execution_metrics SET observed_at = ?",
            (NOW.isoformat(),),
        )
        connection.execute(
            """
            UPDATE opportunity_market_state
            SET end_at = ?, metadata_at = ?, last_observed_at = ?
            """,
            (
                (NOW + timedelta(days=1)).isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            UPDATE opportunity_signals
            SET signal_at = ?, first_seen_at = ?, last_seen_at = ?,
                expires_at = ?, source_observed_at = ?, status = 'active'
            """,
            (
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                (NOW + timedelta(hours=6)).isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            UPDATE opportunity_scanner_state
            SET last_complete_cycle_at = ?, last_complete_discovery_at = ?,
                updated_at = ?
            """,
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            "UPDATE opportunity_catalog_markets SET end_at = ?, metadata_at = ?",
            ((NOW + timedelta(days=1)).isoformat(), NOW.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()
    return signal


def test_keeps_valid_current_live_opportunity(settings):
    _seed(settings, score="85")
    result = _load(settings)
    assert result.status == "ready"
    assert len(result.entries) == 1
    text = format_opportunity_card(result.entries[0].payload, now=NOW)
    assert text.startswith("📈 MOMENTUM СИГНАЛ")
    assert "opportunity_uid" not in text
    assert "event_slug" not in text
    assert "wave_at" not in text
    assert "source_observed_at" not in text
    assert not text.lstrip().startswith("{")


@pytest.mark.parametrize(
    ("overrides", "event_overrides"),
    [
        ({"closed": True}, None),
        ({"resolved": True}, None),
        ({"active": False}, None),
        ({"acceptingOrders": False}, None),
        ({"enableOrderBook": False}, None),
        (None, {"closed": True}),
        (None, {"resolved": True}),
        (None, {"active": False}),
    ],
)
def test_excludes_invalid_gamma_status(settings, overrides, event_overrides):
    _seed(settings, score="85")
    client = _live_gamma_client(
        now=NOW,
        market_overrides=overrides or {},
        event_overrides=event_overrides or {},
    )
    result = _load(settings, gamma_client=client)
    assert result.status == "empty"
    assert result.entries == ()


def test_excludes_missing_end_at(settings):
    _seed(settings, score="85")
    client = _live_gamma_client(
        now=NOW,
        market_overrides={"endDate": None},
        event_overrides={"endDate": None},
    )
    result = _load(settings, gamma_client=client)
    assert result.status == "empty"


def test_excludes_end_at_not_after_now(settings):
    _seed(settings, score="85")
    client = _live_gamma_client(
        now=NOW,
        market_overrides={"endDate": (NOW - timedelta(minutes=1)).isoformat()},
        event_overrides={"endDate": (NOW - timedelta(minutes=1)).isoformat()},
    )
    result = _load(settings, gamma_client=client)
    assert result.status == "empty"


def test_excludes_persisted_end_at_passed(settings):
    _seed(settings, score="85")
    connection = _db(settings)
    try:
        connection.execute(
            "UPDATE opportunity_market_state SET end_at = ?",
            ((NOW - timedelta(hours=1)).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    result = _load(settings)
    assert result.status == "empty"


def test_excludes_stale_signal(settings):
    _seed(settings, score="85")
    stale = NOW - timedelta(seconds=TOP_DISPLAY_FRESHNESS_SECONDS + 10)
    connection = _db(settings)
    try:
        connection.execute(
            """
            UPDATE opportunity_signals
            SET signal_at = ?, source_observed_at = ?
            """,
            (stale.isoformat(), stale.isoformat()),
        )
        connection.execute(
            """
            UPDATE opportunity_metric_buckets
            SET observed_at = ?, book_at = ?, history_at = ?
            """,
            (stale.isoformat(), stale.isoformat(), stale.isoformat()),
        )
        connection.execute(
            "UPDATE opportunity_execution_metrics SET observed_at = ?",
            (stale.isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    result = _load(settings)
    assert result.status == "empty"


def test_excludes_stale_history(settings):
    _seed(settings, score="85")
    stale_history = NOW - timedelta(seconds=TOP_DISPLAY_FRESHNESS_SECONDS + 5)
    connection = _db(settings)
    try:
        connection.execute(
            "UPDATE opportunity_metric_buckets SET history_at = ?",
            (stale_history.isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    result = _load(settings)
    assert result.status == "empty"


def test_excludes_stale_order_book(settings):
    _seed(settings, score="85")
    stale_book = NOW - timedelta(seconds=TOP_DISPLAY_FRESHNESS_SECONDS + 5)
    connection = _db(settings)
    try:
        connection.execute(
            "UPDATE opportunity_metric_buckets SET book_at = ?",
            (stale_book.isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    result = _load(settings)
    assert result.status == "empty"


def test_excludes_gamma_timeout(settings):
    _seed(settings, score="85")
    client = MagicMock()
    client.get_json.side_effect = requests.exceptions.Timeout("slow")
    result = _load(settings, gamma_client=client)
    assert result.status == "empty"


def test_excludes_gamma_provider_error(settings):
    _seed(settings, score="85")
    client = MagicMock()
    response = requests.Response()
    response.status_code = 503
    err = requests.exceptions.HTTPError("boom")
    err.response = response
    client.get_json.side_effect = err
    result = _load(settings, gamma_client=client)
    assert result.status == "empty"


def test_excludes_old_persisted_catalog_entry(settings):
    _seed(settings, score="85")
    # Replace ACTIVE catalog with a different asset universe.
    opportunity_storage.publish_complete_catalog(
        settings,
        markets=[
            ValidatedMarket(
                event_id="other-event",
                market_id="other-market",
                condition_id="other-condition",
                event_slug="other-slug",
                question="Other?",
                category_ids=("politics",),
                mode=ObservationMode.NORMAL,
                liquidity_usd=Decimal("100000"),
                volume_24h_usd=Decimal("250000"),
                metadata_at=NOW,
                end_at=NOW + timedelta(days=1),
                outcomes=(
                    ValidatedOutcome(
                        asset_id="other-asset",
                        name="Yes",
                        price_hint=Decimal("0.55"),
                        index=0,
                    ),
                ),
            )
        ],
        completed_at=NOW,
        discovery_session_id="sess-other",
        pages_completed=1,
    )
    result = _load(settings)
    assert result.status == "empty"


def test_signal_history_still_includes_completed_signals(settings):
    _seed(settings, score="85")
    completed = OpportunitySignal(
        opportunity_uid="opp_hist_done",
        opportunity_key="v1:condition-1:asset-1:EXECUTABLE_MOMENTUM:UP:NORMAL:hist",
        asset_id="asset-1",
        episode_sequence=2,
        material_change_sequence=0,
        family=SignalFamily.EXECUTABLE_MOMENTUM,
        direction=MovementDirection.UP,
        mode=ObservationMode.NORMAL,
        score=Decimal("90"),
        reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
        signal_at=NOW - timedelta(days=4),
        first_seen_at=NOW - timedelta(days=4),
        last_seen_at=NOW - timedelta(days=4),
        expires_at=NOW - timedelta(days=3),
        source_observed_at=NOW - timedelta(days=4),
        status=SignalStatus.EXPIRED,
    )
    save_signal(settings, completed)
    recent = list_recent_signals(settings, limit=8)
    assert any(item.opportunity_uid == "opp_hist_done" for item in recent)
    assert any(item.status == SignalStatus.EXPIRED for item in recent)
    market = load_market_state(settings, "asset-1")
    markets = {
        "asset-1": (
            market.question if market else "Market",
            market.outcome if market else "—",
        )
    }
    text = format_signal_history(recent, markets)
    assert "expired" in text.lower()
    assert "Will it rain?" in text
    top = _load(settings)
    assert all(entry.signal.opportunity_uid != "opp_hist_done" for entry in top.entries)


def test_excluded_opportunity_absent_from_telegram_top_text(settings):
    _seed(settings, score="85")
    client = _live_gamma_client(now=NOW, market_overrides={"closed": True})
    result = _load(settings, gamma_client=client)
    assert result.status == "empty"
    assert result.message
    assert "MARKET OPPORTUNITY" not in result.message


def test_local_filter_records_stable_reason_for_completed_end(settings):
    _seed(settings, score="85")
    signal = opportunity_storage.list_active_signals(settings, limit=1)[0]
    bundle = opportunity_storage.load_persisted_observation_bundle(
        settings,
        asset_id=signal.asset_id,
        observed_at=signal.source_observed_at,
    )
    assert bundle is not None
    connection = _db(settings)
    try:
        connection.execute(
            "UPDATE opportunity_market_state SET end_at = ?",
            ((NOW - timedelta(seconds=1)).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    bundle = opportunity_storage.load_persisted_observation_bundle(
        settings,
        asset_id=signal.asset_id,
        observed_at=signal.source_observed_at,
    )
    decision = evaluate_local_top_eligibility(
        signal=signal,
        bundle=bundle,
        catalog_asset_ids=frozenset({signal.asset_id}),
        now=NOW,
    )
    assert decision.eligible is False
    assert decision.reason is not None
    assert decision.reason.value == "MARKET_EXPIRED"
