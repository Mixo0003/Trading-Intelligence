"""Watchlist subscription-management UX and authorization tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.copy_intel.freshness import filter_alertable_events, run_baseline
from app.copy_intel.watchlist import (
    add_wallet,
    list_entries_for_user,
    profile_url,
    remove_wallet,
    set_monitoring,
)
from app.database import copy_storage
from app.database.storage import initialize_database
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.copy_intel.formatting import (
    format_subscription_card,
    format_unsubscribe_confirm,
    format_watchlist_empty,
)
from app.telegram.copy_intel.handlers import show_watchlist, watchlist_action, watchlist_nav
from app.telegram.inline_menus import (
    CB_COPY_WATCHLIST,
    CB_WL_BACK,
    CB_WL_NEXT,
    CB_WL_PREV,
    empty_watchlist_keyboard,
    watchlist_card_keyboard,
)
from tests.conftest import make_test_settings

W1 = "0xee00ba338c59557141789b127927a55f5cc5cea1"
W2 = "0x1111111111111111111111111111111111111111"
W3 = "0x2222222222222222222222222222222222222222"


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "wl.db"),
        copy_intel_enabled=True,
        admin_ids=[1],
    )
    initialize_database(s)
    return s


def _context(settings, *, user_data=None):
    ctx = MagicMock()
    ctx.application.bot_data = {"settings": settings}
    ctx.user_data = user_data if user_data is not None else {}
    ctx.args = []
    return ctx


def _callback_update(data: str, *, user_id: int = 1):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = data
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=user_id, username="u", first_name="T")
    update.message = None
    return update


def _edit_text(edit: AsyncMock) -> str:
    args = edit.await_args
    assert args is not None
    # edit_or_reply(update, text, reply_markup=...)
    if len(args.args) >= 2:
        return args.args[1]
    return args.kwargs.get("text", "")


def _edit_markup(edit: AsyncMock):
    return edit.await_args.kwargs.get("reply_markup")


class TestWatchlistFormatting:
    def test_empty_state(self):
        text = format_watchlist_empty()
        assert "👀 Watchlist" in text
        assert "No subscribed traders yet" in text
        assert "Trader Discovery" in text
        labels = [b.text for row in empty_watchlist_keyboard().inline_keyboard for b in row]
        assert "🏆 Trader Discovery" in labels
        assert "⬅️ Copy Menu" in labels

    def test_subscription_card_fields(self):
        text = format_subscription_card(
            label="Alpha",
            wallet=W1,
            enabled=True,
            added_at="2026-07-16T12:00:00+00:00",
            baseline_status="COMPLETE",
            last_poll_at="2026-07-16T13:00:00+00:00",
            last_new_event_at=None,
            alert_decisions="FOLLOW_AND_WATCH",
            profile_url=profile_url(W1),
            index=0,
            total=2,
        )
        assert "👤 Alpha" in text
        assert "Monitoring: ON" in text
        assert "Baseline: COMPLETE" in text
        assert "Last poll:" in text
        assert "Last new event: None" in text
        assert "FOLLOW + WATCH" in text
        assert "polymarket.com/profile/" in text

    def test_profile_link_in_keyboard(self):
        url = profile_url(W1)
        kb = watchlist_card_keyboard(W1, index=0, total=1, enabled=True, profile_url=url)
        urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
        assert url in urls
        assert url.startswith("https://polymarket.com/profile/")


class TestWatchlistService:
    def test_pause_and_resume_baseline(self, settings):
        add_wallet(settings, W1, label="A", added_by=1)
        assert set_monitoring(settings, W1, enabled=False, actor="1") == "disabled"
        assert set_monitoring(settings, W1, enabled=False, actor="1") == "already_disabled"
        entry = copy_storage.get_watchlist_entry(settings, W1)
        assert entry is not None and not entry.enabled
        assert W1 not in copy_storage.list_enabled_wallets(settings)

        assert set_monitoring(settings, W1, enabled=True, actor="1") == "enabled"
        state = copy_storage.get_wallet_monitor_state(settings, W1)
        assert state is not None
        assert state.baseline_status == "PENDING"
        assert set_monitoring(settings, W1, enabled=True, actor="1") == "already_enabled"

    def test_resume_emits_zero_historical_alerts(self, settings):
        from app.copy_intel.ingestion import ingest_wallet

        add_wallet(settings, W1, added_by=1)
        pos = {
            "conditionId": "cond-1",
            "asset": "t1",
            "outcome": "Yes",
            "size": 10.0,
            "avgPrice": 0.4,
            "curPrice": 0.5,
            "title": "M",
            "slug": "m",
            "eventSlug": "e",
        }
        client = MagicMock()
        client.fetch_positions.return_value = [pos]
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        set_monitoring(settings, W1, enabled=False, actor="1")
        set_monitoring(settings, W1, enabled=True, actor="1")
        assert copy_storage.get_wallet_monitor_state(settings, W1).baseline_status == "PENDING"
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        events = ingest_wallet(settings, W1, client=client)
        assert filter_alertable_events(settings, W1, events) == []

    def test_unsubscribe_idempotent(self, settings):
        add_wallet(settings, W1, added_by=1)
        assert remove_wallet(settings, W1, actor="1") is True
        assert remove_wallet(settings, W1, actor="1") is False
        assert copy_storage.get_watchlist_entry(settings, W1) is None

    def test_user_isolation(self, tmp_path):
        s = make_test_settings(
            database_path=str(tmp_path / "iso.db"),
            copy_intel_enabled=True,
            admin_ids=[1, 2, 3],
        )
        initialize_database(s)
        add_wallet(s, W1, label="mine", added_by=2)
        add_wallet(s, W2, label="other", added_by=3)
        mine = list_entries_for_user(s, 2, is_admin=True)
        assert [e.wallet for e in mine] == [W1]
        admin1 = list_entries_for_user(s, 1, is_admin=True)
        assert admin1 == []


@pytest.mark.asyncio
class TestWatchlistHandlers:
    async def test_empty_watchlist_screen(self, settings):
        update = _callback_update(CB_COPY_WATCHLIST, user_id=1)
        ctx = _context(settings)
        with patch(
            "app.telegram.copy_intel.handlers.edit_or_reply", new_callable=AsyncMock
        ) as edit:
            await show_watchlist(update, ctx, edit=True)
            text = _edit_text(edit)
            markup = _edit_markup(edit)
        assert "No subscribed traders yet" in text
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert "🏆 Trader Discovery" in labels

    async def test_populated_and_pagination(self, settings):
        add_wallet(settings, W1, label="One", added_by=1)
        add_wallet(settings, W2, label="Two", added_by=1)
        add_wallet(settings, W3, label="Three", added_by=1)
        update = _callback_update(CB_COPY_WATCHLIST, user_id=1)
        ctx = _context(settings)
        with patch(
            "app.telegram.copy_intel.handlers.edit_or_reply", new_callable=AsyncMock
        ) as edit:
            await show_watchlist(update, ctx, edit=True)
            text = _edit_text(edit)
            assert "One" in text or "0xee00" in text
            assert "(1/3)" in text
            await watchlist_nav(update, ctx, CB_WL_NEXT)
            text = _edit_text(edit)
            assert "(2/3)" in text
            await watchlist_nav(update, ctx, CB_WL_NEXT)
            text = _edit_text(edit)
            assert "(3/3)" in text
            await watchlist_nav(update, ctx, CB_WL_PREV)
            text = _edit_text(edit)
            assert "(2/3)" in text

    async def test_pause_resume_via_callback(self, settings):
        add_wallet(settings, W1, label="A", added_by=1)
        update = _callback_update(f"copy:wl:pause:{W1}", user_id=1)
        ctx = _context(settings)
        with patch(
            "app.telegram.copy_intel.handlers.edit_or_reply", new_callable=AsyncMock
        ) as edit:
            await watchlist_action(update, ctx, f"copy:wl:pause:{W1}")
            text = _edit_text(edit)
            assert "PAUSED" in text
            await watchlist_action(update, ctx, f"copy:wl:resume:{W1}")
            text = _edit_text(edit)
            assert "ON" in text
        state = copy_storage.get_wallet_monitor_state(settings, W1)
        assert state is not None
        assert state.baseline_status == "PENDING"

    async def test_unsubscribe_confirm_and_cancel(self, settings):
        add_wallet(settings, W1, label="A", added_by=1)
        update = _callback_update(f"copy:wl:rmq:{W1}", user_id=1)
        ctx = _context(settings)
        with patch(
            "app.telegram.copy_intel.handlers.edit_or_reply", new_callable=AsyncMock
        ) as edit:
            await watchlist_action(update, ctx, f"copy:wl:rmq:{W1}")
            text = _edit_text(edit)
            assert "Unsubscribe" in text
            assert "A" in text or "0xee00" in text
            await watchlist_action(update, ctx, f"copy:wl:rmn:{W1}")
        assert copy_storage.get_watchlist_entry(settings, W1) is not None

    async def test_unsubscribe_removes_from_active_list(self, settings):
        add_wallet(settings, W1, label="A", added_by=1)
        update = _callback_update(f"copy:wl:rmy:{W1}", user_id=1)
        ctx = _context(settings)
        with patch("app.telegram.copy_intel.handlers.edit_or_reply", new_callable=AsyncMock):
            await watchlist_action(update, ctx, f"copy:wl:rmy:{W1}")
            # repeated unsubscribe safe
            await watchlist_action(update, ctx, f"copy:wl:rmy:{W1}")
        assert copy_storage.get_watchlist_entry(settings, W1) is None

    async def test_cannot_manage_other_user_subscription(self, tmp_path):
        s = make_test_settings(
            database_path=str(tmp_path / "own.db"),
            copy_intel_enabled=True,
            admin_ids=[1, 2, 3],
        )
        initialize_database(s)
        add_wallet(s, W1, label="Owner", added_by=2)
        update = _callback_update(f"copy:wl:pause:{W1}", user_id=3)
        ctx = _context(s)
        with patch(
            "app.telegram.copy_intel.handlers.edit_or_reply", new_callable=AsyncMock
        ) as edit:
            await watchlist_action(update, ctx, f"copy:wl:pause:{W1}")
            text = _edit_text(edit)
            assert "Not authorized" in text
        entry = copy_storage.get_watchlist_entry(s, W1)
        assert entry is not None and entry.enabled

    async def test_callback_query_answered(self, settings):
        add_wallet(settings, W1, label="A", added_by=1)
        update = _callback_update(CB_COPY_WATCHLIST, user_id=1)
        ctx = _context(settings)
        with patch("app.telegram.copy_intel.handlers.show_watchlist", new_callable=AsyncMock):
            await inline_callback_handler(update, ctx)
        update.callback_query.answer.assert_awaited()

    async def test_back_returns_to_copy_menu(self, settings):
        update = _callback_update(CB_WL_BACK, user_id=1)
        ctx = _context(settings)
        with patch(
            "app.telegram.copy_intel.handlers.edit_or_reply", new_callable=AsyncMock
        ) as edit:
            await watchlist_nav(update, ctx, CB_WL_BACK)
            assert "Copy" in _edit_text(edit)


def test_unsubscribe_confirm_copy():
    text = format_unsubscribe_confirm(label="Alpha", wallet=W1)
    assert "Unsubscribe" in text
    assert "Alpha" in text
