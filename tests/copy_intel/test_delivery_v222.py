"""V2.2.2 Checkpoint 9 — owner-admin delivery routing, privacy, claim, batch retry."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.copy_intel.delivery import (
    CODE_OWNERSHIP_CHANGED,
    CODE_RECIPIENT_NOT_ADMIN,
    CODE_RECIPIENT_UNBOUND,
    CODE_RETRY_EXHAUSTED,
    MAX_SEND_ATTEMPTS,
    STATUS_DEAD_LETTER,
    STATUS_RETRY_WAIT,
    STATUS_SENT,
    STATUS_SUPPRESSED,
    compute_next_attempt_at,
    deliver_pending_alerts,
    recheck_recipient_authorization,
    resolve_owner_recipient,
    retry_due_alert_deliveries,
)
from app.copy_intel.models import CopyRecommendation
from app.copy_intel.paper_commit import prepare_pending_recommendation
from app.copy_intel.settings_profiles import update_setting
from app.copy_intel.watchlist import add_wallet, can_manage_entry, list_entries_for_user
from app.database import copy_storage
from app.database.storage import get_connection, initialize_database
from app.telegram.auth import ACCESS_DENIED
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.copy_intel.handlers import copy_command
from app.telegram.inline_menus import CB_COPY_ALERTS
from tests.conftest import make_test_settings

WALLET_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ADMIN_A = 101
ADMIN_B = 202
NON_ADMIN = 303


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "cp9_delivery.db"),
        copy_intel_enabled=True,
        copy_paper_bankroll_usd=10_000.0,
        admin_ids=[ADMIN_A, ADMIN_B],
    )
    initialize_database(s)
    return s


def _rec(**overrides) -> CopyRecommendation:
    base = dict(
        wallet=WALLET_A,
        trader_label="lab",
        market_title="Sanitized",
        market_slug="sanitized-market",
        outcome="Yes",
        event_type="open",
        entry_price=0.5,
        current_price=0.51,
        price_drift=0.02,
        slippage_est=0.01,
        trader_score=70.0,
        max_alloc=100.0,
        action="COPY",
        reason="copy_eligible",
        event_uid="ff1:" + ("aa" * 32),
        first_fill_ts=1_700_000_000,
        last_fill_ts=1_700_000_000,
        trader_vwap=0.50,
        midpoint=0.505,
        best_ask=0.51,
        execution_vwap=0.51,
        slippage=0.01,
        visible_ask_depth_usd=1240.0,
        proposed_allocation_usd=100.0,
        paper_bankroll_usd=10_000.0,
        paper_bankroll_share=0.01,
        resulting_position_size=196.0,
        size_delta=100.0,
        reason_codes=("copy_eligible",),
    )
    base.update(overrides)
    return CopyRecommendation(**base)


def _context(settings):
    ctx = MagicMock()
    ctx.application.bot_data = {"settings": settings}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.user_data = {}
    return ctx


def _callback_update(data: str, *, user_id: int):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = data
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=user_id, username="u", first_name="T")
    update.message = None
    return update


class TestOwnerRouting:
    @pytest.mark.asyncio
    async def test_owner_receives_own_copy(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        update_setting(settings, ADMIN_A, "alert_decisions", "FOLLOW_ONLY")
        rec = _rec(action="COPY", wallet=WALLET_A)
        ctx = _context(settings)
        await deliver_pending_alerts(
            ctx, settings, [("uid-copy-a", WALLET_A, rec, None)]
        )
        assert ctx.bot.send_message.await_count == 1
        assert ctx.bot.send_message.await_args.kwargs["chat_id"] == ADMIN_A
        row = copy_storage.get_copy_alert_delivery(
            settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid="uid-copy-a"
        )
        assert row is not None
        assert row["status"] == STATUS_SENT

    @pytest.mark.asyncio
    async def test_owner_receives_watch_when_follow_and_watch(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        update_setting(settings, ADMIN_A, "alert_decisions", "FOLLOW_AND_WATCH")
        rec = _rec(action="WATCH", wallet=WALLET_A, event_uid="uid-watch")
        ctx = _context(settings)
        await deliver_pending_alerts(ctx, settings, [("uid-watch", WALLET_A, rec, None)])
        assert ctx.bot.send_message.await_count == 1
        assert copy_storage.count_open_paper_positions(settings) == 0

    @pytest.mark.asyncio
    async def test_follow_only_suppresses_watch(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        update_setting(settings, ADMIN_A, "alert_decisions", "FOLLOW_ONLY")
        rec = _rec(action="WATCH", wallet=WALLET_A, event_uid="uid-watch2")
        ctx = _context(settings)
        await deliver_pending_alerts(ctx, settings, [("uid-watch2", WALLET_A, rec, None)])
        assert ctx.bot.send_message.await_count == 0
        assert (
            copy_storage.get_copy_alert_delivery(
                settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid="uid-watch2"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_skip_never_claimed_or_sent(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        rec = _rec(action="SKIP", wallet=WALLET_A, event_uid="uid-skip")
        include, _ = prepare_pending_recommendation(
            settings, rec, condition_id="c", asset="a"
        )
        assert include is False
        ctx = _context(settings)
        await deliver_pending_alerts(ctx, settings, [("uid-skip", WALLET_A, rec, None)])
        assert ctx.bot.send_message.await_count == 0
        assert (
            copy_storage.get_copy_alert_delivery(
                settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid="uid-skip"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_admin_a_never_receives_admin_b_event(self, settings):
        add_wallet(settings, WALLET_B, added_by=ADMIN_B)
        update_setting(settings, ADMIN_B, "alert_decisions", "FOLLOW_AND_WATCH")
        rec = _rec(action="COPY", wallet=WALLET_B, event_uid="uid-b")
        ctx = _context(settings)
        await deliver_pending_alerts(ctx, settings, [("uid-b", WALLET_B, rec, None)])
        assert ctx.bot.send_message.await_count == 1
        assert ctx.bot.send_message.await_args.kwargs["chat_id"] == ADMIN_B
        assert (
            copy_storage.get_copy_alert_delivery(
                settings, subscriber_id=ADMIN_A, wallet=WALLET_B, event_uid="uid-b"
            )
            is None
        )


class TestPrivacyAndAuth:
    def test_admin_a_cannot_read_admin_b_watchlist(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        add_wallet(settings, WALLET_B, added_by=ADMIN_B)
        a_view = list_entries_for_user(settings, ADMIN_A, is_admin=True)
        b_view = list_entries_for_user(settings, ADMIN_B, is_admin=True)
        assert [e.wallet for e in a_view] == [WALLET_A]
        assert [e.wallet for e in b_view] == [WALLET_B]
        assert (
            can_manage_entry(
                copy_storage.get_watchlist_entry(settings, WALLET_B),
                ADMIN_A,
                is_admin=True,
            )
            is False
        )

    def test_admin_a_cannot_read_admin_b_recommendations_or_paper(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        add_wallet(settings, WALLET_B, added_by=ADMIN_B)
        copy_storage.save_recommendation_envelope(
            settings,
            {
                "wallet": WALLET_B,
                "action": "COPY",
                "event_uid": "rec-b",
                "max_alloc": 10,
                "reason": "x",
            },
        )
        copy_storage.open_paper_position(
            settings,
            wallet=WALLET_B,
            condition_id="c",
            market_slug="m",
            outcome="Yes",
            notional=10.0,
            entry_price=0.5,
            asset="tok",
            source_event_uid="paper-b",
        )
        assert copy_storage.get_recent_recommendations_for_owner(settings, ADMIN_A) == []
        assert copy_storage.paper_exposure_totals_for_owner(settings, ADMIN_A)["total"] == 0
        assert copy_storage.paper_exposure_totals_for_owner(settings, ADMIN_B)["total"] == 10

    @pytest.mark.asyncio
    async def test_non_admin_cannot_access_copy_handlers(self, settings):
        update = MagicMock()
        update.effective_user = MagicMock(id=NON_ADMIN)
        update.effective_message = AsyncMock()
        update.message = update.effective_message
        update.callback_query = None
        ctx = _context(settings)
        await copy_command(update, ctx)
        update.effective_message.reply_text.assert_awaited()
        assert ACCESS_DENIED in update.effective_message.reply_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_callback_dispatch_rechecks_admin(self, settings):
        update = _callback_update(CB_COPY_ALERTS, user_id=NON_ADMIN)
        ctx = _context(settings)
        with patch(
            "app.telegram.callback_handler.edit_or_reply", new_callable=AsyncMock
        ):
            await inline_callback_handler(update, ctx)
        update.effective_message.reply_text.assert_awaited()
        assert ACCESS_DENIED in update.effective_message.reply_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_callback_row_operation_rechecks_ownership(self, settings):
        add_wallet(settings, WALLET_B, added_by=ADMIN_B)
        update = _callback_update(f"copy:wl:pause:{WALLET_B}", user_id=ADMIN_A)
        ctx = _context(settings)
        with patch(
            "app.telegram.copy_intel.handlers.edit_or_reply", new_callable=AsyncMock
        ) as edit:
            from app.telegram.copy_intel.handlers import watchlist_action

            await watchlist_action(update, ctx, f"copy:wl:pause:{WALLET_B}")
            text = edit.await_args.args[1] if edit.await_args else ""
        assert "Not authorized" in text or "authorized" in text.lower()
        entry = copy_storage.get_watchlist_entry(settings, WALLET_B)
        assert entry is not None and entry.enabled is True


class TestRecipientDiagnostics:
    @pytest.mark.asyncio
    async def test_added_by_null_recipient_unbound_no_send(self, settings):
        copy_storage.add_watchlist_wallet(settings, WALLET_A, label="x", added_by=None)
        res = resolve_owner_recipient(settings, WALLET_A)
        assert res.code == CODE_RECIPIENT_UNBOUND
        rec = _rec(wallet=WALLET_A, event_uid="uid-unbound")
        ctx = _context(settings)
        await deliver_pending_alerts(ctx, settings, [("uid-unbound", WALLET_A, rec, None)])
        assert ctx.bot.send_message.await_count == 0

    @pytest.mark.asyncio
    async def test_removed_admin_recipient_not_admin(self, tmp_path):
        s = make_test_settings(
            database_path=str(tmp_path / "revoked.db"),
            admin_ids=[ADMIN_A, ADMIN_B],
        )
        initialize_database(s)
        add_wallet(s, WALLET_A, added_by=ADMIN_A)
        s2 = make_test_settings(
            database_path=s.database_path,
            admin_ids=[ADMIN_B],
        )
        res = resolve_owner_recipient(s2, WALLET_A)
        assert res.code == CODE_RECIPIENT_NOT_ADMIN
        rec = _rec(wallet=WALLET_A, event_uid="uid-revoked")
        ctx = _context(s2)
        await deliver_pending_alerts(ctx, s2, [("uid-revoked", WALLET_A, rec, None)])
        assert ctx.bot.send_message.await_count == 0

    @pytest.mark.asyncio
    async def test_changed_ownership_before_send_suppresses(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        card = "CARD"
        assert copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid="uid-own",
            payload_text=card,
        )
        with get_connection(settings) as conn:
            conn.execute(
                "UPDATE copy_watchlist SET added_by = ? WHERE wallet = ?",
                (ADMIN_B, WALLET_A),
            )
        auth = recheck_recipient_authorization(
            settings, wallet=WALLET_A, subscriber_id=ADMIN_A
        )
        assert auth.ok is False
        ctx = _context(settings)
        from app.copy_intel.delivery import _send_claimed_batches

        await _send_claimed_batches(
            settings,
            recipient_id=ADMIN_A,
            cards_with_keys=[(WALLET_A, "uid-own", card)],
            send=ctx.bot.send_message,
            now=None,
        )
        row = copy_storage.get_copy_alert_delivery(
            settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid="uid-own"
        )
        assert row["status"] == STATUS_SUPPRESSED
        assert ctx.bot.send_message.await_count == 0


class TestPaperGate:
    def test_failed_paper_commit_no_delivery_claim(self, settings):
        rec = _rec(action="COPY", execution_vwap=None, event_uid="uid-nopaper")
        include, result = prepare_pending_recommendation(
            settings, rec, condition_id="c", asset="tok"
        )
        assert include is False
        assert result is not None and result.ok is False

    def test_watch_creates_no_paper_but_may_be_pending(self, settings):
        rec = _rec(action="WATCH", event_uid="uid-w")
        include, paper = prepare_pending_recommendation(
            settings, rec, condition_id="c", asset="tok"
        )
        assert include is True
        assert paper is None
        assert copy_storage.count_open_paper_positions(settings) == 0


class TestClaimIdempotency:
    def test_atomic_claim_prevents_duplicate_concurrent(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        uid = "uid-conc"

        def once() -> bool:
            return copy_storage.claim_copy_alert_delivery(
                settings,
                subscriber_id=ADMIN_A,
                wallet=WALLET_A,
                event_uid=uid,
                payload_text="x",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: once(), range(8)))
        assert sum(1 for r in results if r) == 1

    def test_sent_delivery_never_reclaimed(self, settings):
        assert copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid="uid-sent",
            payload_text="x",
        )
        copy_storage.mark_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid="uid-sent",
            status=STATUS_SENT,
        )
        assert not copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid="uid-sent",
            payload_text="x",
            allow_retry=True,
        )

    def test_retry_reuses_same_delivery_record(self, settings):
        assert copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid="uid-retry",
            payload_text="x",
        )
        copy_storage.mark_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid="uid-retry",
            status=STATUS_RETRY_WAIT,
            next_attempt_at="1970-01-01T00:00:00+00:00",
            last_error_code="telegram_send_failed",
        )
        assert copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid="uid-retry",
            payload_text="x",
            now=1_700_000_000,
        )
        with get_connection(settings) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM copy_alert_deliveries WHERE event_uid = ?",
                ("uid-retry",),
            ).fetchone()["c"]
        assert count == 1
        row = copy_storage.get_copy_alert_delivery(
            settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid="uid-retry"
        )
        assert int(row["attempt_count"]) == 2


class TestBatchPartialFailure:
    @pytest.mark.asyncio
    async def test_first_batch_sent_second_fails_no_duplicate(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        with patch("app.copy_intel.delivery.TELEGRAM_MAX_MESSAGE_LENGTH", 80):
            card1 = "CARDONE-" + ("x" * 40)
            card2 = "CARDTWO-" + ("y" * 40)
            assert copy_storage.claim_copy_alert_delivery(
                settings,
                subscriber_id=ADMIN_A,
                wallet=WALLET_A,
                event_uid="uid-b1",
                payload_text=card1,
            )
            assert copy_storage.claim_copy_alert_delivery(
                settings,
                subscriber_id=ADMIN_A,
                wallet=WALLET_A,
                event_uid="uid-b2",
                payload_text=card2,
            )
            send = AsyncMock(side_effect=[None, RuntimeError("boom")])
            from app.copy_intel.delivery import _send_claimed_batches

            await _send_claimed_batches(
                settings,
                recipient_id=ADMIN_A,
                cards_with_keys=[
                    (WALLET_A, "uid-b1", card1),
                    (WALLET_A, "uid-b2", card2),
                ],
                send=send,
                now=1_700_000_000,
            )
        r1 = copy_storage.get_copy_alert_delivery(
            settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid="uid-b1"
        )
        r2 = copy_storage.get_copy_alert_delivery(
            settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid="uid-b2"
        )
        assert r1["status"] == STATUS_SENT
        assert r2["status"] == STATUS_RETRY_WAIT
        assert send.await_count == 2


class TestRetrySchedule:
    def test_retry_delays_1_5_15_and_dead_letter(self):
        now = 1_700_000_000.0
        a1, term1 = compute_next_attempt_at(1, now=now)
        a2, term2 = compute_next_attempt_at(2, now=now)
        a3, term3 = compute_next_attempt_at(3, now=now)
        assert term1 is None and term2 is None
        assert term3 == CODE_RETRY_EXHAUSTED
        t1 = datetime.fromisoformat(a1)
        t2 = datetime.fromisoformat(a2)
        assert abs((t1.timestamp() - now) - 60) < 1
        assert abs((t2.timestamp() - now) - 300) < 1
        assert a3 is None

    def test_retry_after_honored_within_bound(self):
        now = 1_700_000_000.0
        nxt, term = compute_next_attempt_at(1, now=now, retry_after_seconds=120)
        assert term is None
        assert abs(datetime.fromisoformat(nxt).timestamp() - now - 120) < 1
        nxt2, _ = compute_next_attempt_at(1, now=now, retry_after_seconds=10_000)
        assert abs(datetime.fromisoformat(nxt2).timestamp() - now - 3600) < 1

    @pytest.mark.asyncio
    async def test_max_attempts_dead_letter_and_fields(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        uid = "uid-dl"
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            assert copy_storage.claim_copy_alert_delivery(
                settings,
                subscriber_id=ADMIN_A,
                wallet=WALLET_A,
                event_uid=uid,
                payload_text="CARD",
                now=1_700_000_000 + attempt * 10_000,
            )
            send = AsyncMock(side_effect=RuntimeError("fail"))
            from app.copy_intel.delivery import _send_claimed_batches

            await _send_claimed_batches(
                settings,
                recipient_id=ADMIN_A,
                cards_with_keys=[(WALLET_A, uid, "CARD")],
                send=send,
                now=1_700_000_000 + attempt * 10_000,
            )
            row = copy_storage.get_copy_alert_delivery(
                settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid=uid
            )
            if attempt < MAX_SEND_ATTEMPTS:
                assert row["status"] == STATUS_RETRY_WAIT
                assert row["last_error_code"] == "telegram_send_failed"
                assert row["next_attempt_at"]
            else:
                assert row["status"] == STATUS_DEAD_LETTER
                assert row["last_error_code"] == CODE_RETRY_EXHAUSTED

    @pytest.mark.asyncio
    async def test_unauthorized_retry_becomes_suppressed(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        uid = "uid-sup"
        assert copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid=uid,
            payload_text="CARD",
        )
        copy_storage.mark_copy_alert_delivery(
            settings,
            subscriber_id=ADMIN_A,
            wallet=WALLET_A,
            event_uid=uid,
            status=STATUS_RETRY_WAIT,
            next_attempt_at="1970-01-01T00:00:00+00:00",
            last_error_code="telegram_send_failed",
        )
        with get_connection(settings) as conn:
            conn.execute(
                "UPDATE copy_watchlist SET added_by = ? WHERE wallet = ?",
                (ADMIN_B, WALLET_A),
            )
        ctx = _context(settings)
        await retry_due_alert_deliveries(ctx, settings, now=1_700_000_000)
        row = copy_storage.get_copy_alert_delivery(
            settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid=uid
        )
        assert row["status"] == STATUS_SUPPRESSED
        assert row["last_error_code"] in {
            CODE_OWNERSHIP_CHANGED,
            "ownership_changed",
            "recipient_no_longer_owner",
        }
        assert ctx.bot.send_message.await_count == 0

    def test_no_raw_telegram_or_token_in_diagnostics(self, settings):
        copy_storage.write_audit_log(
            settings,
            actor="copy_delivery",
            action="delivery_diagnostic",
            payload={"code": CODE_RECIPIENT_UNBOUND, "wallet": WALLET_A[:16]},
        )
        with get_connection(settings) as conn:
            payload = conn.execute(
                "SELECT payload_json FROM copy_audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload_json"]
        assert "token" not in payload.lower()
        assert "Authorization" not in payload
        data = json.loads(payload)
        assert data["code"] == CODE_RECIPIENT_UNBOUND


class TestClaimBeforeSend:
    @pytest.mark.asyncio
    async def test_no_send_before_claim_and_sent_only_after_success(self, settings):
        add_wallet(settings, WALLET_A, added_by=ADMIN_A)
        rec = _rec(event_uid="uid-order")
        send = AsyncMock()
        claimed = {"n": 0}
        real_claim = copy_storage.claim_copy_alert_delivery

        def wrapped_claim(*args, **kwargs):
            claimed["n"] += 1
            assert send.await_count == 0
            return real_claim(*args, **kwargs)

        with patch(
            "app.copy_intel.delivery.copy_storage.claim_copy_alert_delivery",
            side_effect=wrapped_claim,
        ):
            ctx = _context(settings)
            ctx.bot.send_message = send
            await deliver_pending_alerts(
                ctx, settings, [("uid-order", WALLET_A, rec, None)]
            )
        assert claimed["n"] >= 1
        assert send.await_count == 1
        row = copy_storage.get_copy_alert_delivery(
            settings, subscriber_id=ADMIN_A, wallet=WALLET_A, event_uid="uid-order"
        )
        assert row["status"] == STATUS_SENT
        assert row["sent_at"]


class TestSchedulerJobUnchanged:
    def test_exactly_one_global_scheduler_job_registration(self):
        from app.copy_intel.scheduler import COPY_POLL_REGISTERED_KEY, schedule_copy_intel_jobs

        s = make_test_settings(copy_intel_enabled=True)
        app = MagicMock()
        app.bot_data = {}
        app.job_queue = MagicMock()
        schedule_copy_intel_jobs(app, s)
        schedule_copy_intel_jobs(app, s)
        assert app.bot_data[COPY_POLL_REGISTERED_KEY] is True
        app.job_queue.run_repeating.assert_called_once()
