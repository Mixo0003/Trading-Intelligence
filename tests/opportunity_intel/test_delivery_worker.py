"""Checkpoint 7 — delivery worker (no live Telegram calls)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.database.opportunity_storage import (
    initialize_opportunity_schema,
    insert_delivery_wave,
    list_deliveries_for_opportunity,
    load_delivery,
    save_delivery_gates,
    save_subscription,
)
from app.database.storage import initialize_database, upsert_user
from app.opportunity_intel.delivery_gates import OpportunityDeliveryGates
from app.opportunity_intel.delivery_worker import process_opportunity_deliveries
from app.opportunity_intel.models import (
    DeliveryStatus,
    OpportunityDelivery,
)
from tests.conftest import make_test_settings
from tests.opportunity_intel.test_delivery_wave import (
    _instant_sub,
    _seed_signal_and_metrics,
)
NOW = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
ADMIN_ID = 1
UID = "opp_worker_1"


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opp_worker.db"),
        admin_ids=[ADMIN_ID],
        opportunity_auto_alerts_enabled=True,
        opportunity_user_delivery_enabled=False,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    upsert_user(value, ADMIN_ID, "admin", "Admin")
    return value


def _seed_pending(settings, *, uid: str = UID, telegram_id: int = ADMIN_ID) -> None:
    _seed_signal_and_metrics(settings, uid=uid)
    save_subscription(
        settings,
        _instant_sub(telegram_id),
        expected_revision=None,
    )
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(
            admin_delivery_enabled=True,
            user_delivery_enabled=False,
        ),
    )
    insert_delivery_wave(
        settings,
        (
            OpportunityDelivery(
                telegram_id=telegram_id,
                opportunity_uid=uid,
                status=DeliveryStatus.PENDING,
                payload_text="paper alert card",
                signal_at=NOW,
                wave_at=NOW,
                settings_revision=0,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_auto_alerts_disabled_sends_nothing(settings):
    _seed_pending(settings)
    send = AsyncMock()
    result = await process_opportunity_deliveries(
        settings,
        send_message=send,
        now=NOW,
        auto_alerts_enabled=False,
    )
    assert result.processed == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_send_success(settings):
    _seed_pending(settings)
    send = AsyncMock(return_value=None)
    result = await process_opportunity_deliveries(
        settings, send_message=send, now=NOW
    )
    assert result.sent == 1
    send.assert_awaited_once()
    row = load_delivery(settings, ADMIN_ID, UID)
    assert row is not None
    assert row.status == DeliveryStatus.SENT


@pytest.mark.asyncio
async def test_fresh_trade_candidate_card_replaces_legacy_payload(
    settings, monkeypatch
):
    _seed_pending(settings)
    monkeypatch.setattr(
        "app.opportunity_intel.delivery_worker.render_fresh_trade_candidate_card",
        lambda *_args, **_kwargs: "POLYMARKET TRADE CANDIDATE\n\nРішення: WAIT",
    )
    send = AsyncMock(return_value=None)

    result = await process_opportunity_deliveries(
        settings,
        send_message=send,
        now=NOW,
        fresh_trade_candidate_cards=True,
    )

    assert result.sent == 1
    assert send.await_args.kwargs["text"].startswith("POLYMARKET TRADE CANDIDATE")
    assert "MARKET OPPORTUNITY" not in send.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_retryable_failure(settings):
    _seed_pending(settings)

    class TimedOut(Exception):
        pass

    TimedOut.__name__ = "TimedOut"
    send = AsyncMock(side_effect=TimedOut("timeout"))
    result = await process_opportunity_deliveries(
        settings, send_message=send, now=NOW
    )
    assert result.retry_wait == 1
    row = load_delivery(settings, ADMIN_ID, UID)
    assert row is not None
    assert row.status == DeliveryStatus.RETRY_WAIT


@pytest.mark.asyncio
async def test_permanent_failure_dead_letter(settings):
    _seed_pending(settings)

    class Forbidden(Exception):
        pass

    Forbidden.__name__ = "Forbidden"
    send = AsyncMock(side_effect=Forbidden("blocked"))
    result = await process_opportunity_deliveries(
        settings, send_message=send, now=NOW
    )
    assert result.dead_letter == 1
    row = load_delivery(settings, ADMIN_ID, UID)
    assert row is not None
    assert row.status == DeliveryStatus.DEAD_LETTER


@pytest.mark.asyncio
async def test_expired_claim_recovery(settings):
    _seed_pending(settings)
    from app.database.opportunity_storage import _iso, _opportunity_connection

    with _opportunity_connection(settings) as connection:
        connection.execute(
            """
            UPDATE opportunity_deliveries
            SET status = 'claimed',
                claim_id = 'old-claim',
                claimed_at = ?,
                claim_expires_at = ?,
                attempt_count = 1
            WHERE telegram_id = ? AND opportunity_uid = ?
            """,
            (
                _iso(NOW - timedelta(minutes=10)),
                _iso(NOW - timedelta(minutes=5)),
                ADMIN_ID,
                UID,
            ),
        )
    send = AsyncMock(return_value=None)
    result = await process_opportunity_deliveries(
        settings, send_message=send, now=NOW
    )
    assert result.sent == 1
    send.assert_awaited()


@pytest.mark.asyncio
async def test_authorization_change_suppresses(settings):
    _seed_pending(settings)
    save_subscription(
        settings,
        _instant_sub(ADMIN_ID, enabled=False, revision=1),
        expected_revision=0,
    )
    send = AsyncMock()
    result = await process_opportunity_deliveries(
        settings, send_message=send, now=NOW
    )
    assert result.suppressed == 1
    send.assert_not_awaited()
    row = load_delivery(settings, ADMIN_ID, UID)
    assert row is not None
    assert row.status == DeliveryStatus.SUPPRESSED


@pytest.mark.asyncio
async def test_user_gate_change_suppresses_non_admin(settings):
    upsert_user(settings, 2002, "user", "User")
    _seed_signal_and_metrics(settings, uid=UID)
    save_subscription(
        settings,
        _instant_sub(2002),
        expected_revision=None,
    )
    insert_delivery_wave(
        settings,
        (
            OpportunityDelivery(
                telegram_id=2002,
                opportunity_uid=UID,
                status=DeliveryStatus.PENDING,
                payload_text="user card",
                signal_at=NOW,
                wave_at=NOW,
                settings_revision=0,
            ),
        ),
    )
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(
            admin_delivery_enabled=True,
            user_delivery_enabled=False,
        ),
    )
    send = AsyncMock()
    await process_opportunity_deliveries(settings, send_message=send, now=NOW)
    user_row = load_delivery(settings, 2002, UID)
    assert user_row is not None
    assert user_row.status == DeliveryStatus.SUPPRESSED


@pytest.mark.asyncio
async def test_bounded_batch_and_no_live_telegram(settings):
    _seed_pending(settings)
    send = AsyncMock(return_value=None)
    result = await process_opportunity_deliveries(
        settings,
        send_message=send,
        now=NOW,
        batch_size=1,
        concurrency=1,
    )
    assert result.processed <= 1
    assert send.await_count <= 1


@pytest.mark.asyncio
async def test_max_attempts_dead_letter(settings):
    _seed_pending(settings)
    from app.database.opportunity_storage import _opportunity_connection

    with _opportunity_connection(settings) as connection:
        connection.execute(
            """
            UPDATE opportunity_deliveries
            SET attempt_count = 3, status = 'pending'
            WHERE telegram_id = ? AND opportunity_uid = ?
            """,
            (ADMIN_ID, UID),
        )
    send = AsyncMock()
    result = await process_opportunity_deliveries(
        settings, send_message=send, now=NOW
    )
    assert result.processed == 0
    send.assert_not_awaited()
    rows = list_deliveries_for_opportunity(settings, UID)
    assert rows
