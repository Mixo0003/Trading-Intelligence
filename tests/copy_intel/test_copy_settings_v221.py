"""Focused tests for V2.2.1 editable Copy Settings."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.copy_intel.freshness import filter_alertable_events
from app.copy_intel.models import TradeEvent
from app.copy_intel.scheduler import (
    hard_filter_max_event_age_seconds,
    should_skip_wallet_poll,
    _event_too_old_for_subscriber,
    _subscriber_allows_recommendation,
)
from app.copy_intel.settings_bounds import parse_and_validate
from app.copy_intel.settings_confirm import (
    clear_confirmations_for_tests,
    create_edit_confirmation,
    create_reset_confirmation,
    execute_confirmed_settings,
)
from app.copy_intel.settings_profiles import (
    build_settings,
    get_user_settings,
    preview_setting_change,
    set_profile,
    settings_to_payload,
    update_setting,
)
from app.copy_intel.watchlist import add_wallet
from app.database.storage import initialize_database
from app.database import copy_storage
from tests.conftest import make_test_settings


@pytest.fixture(autouse=True)
def _clear_tokens():
    clear_confirmations_for_tests()
    yield
    clear_confirmations_for_tests()


class TestBounds:
    def test_reject_out_of_range(self):
        assert parse_and_validate("min_resolved_markets", 2).ok is False
        assert parse_and_validate("min_resolved_markets", 501).ok is False
        assert parse_and_validate("poll_interval_seconds", 30).ok is False
        assert parse_and_validate("max_event_age_seconds", 10).ok is False
        assert parse_and_validate("max_drawdown_pct", 0.01).ok is False

    def test_accept_percent_forms(self):
        r = parse_and_validate("max_drawdown_pct", "40%")
        assert r.ok and r.value == pytest.approx(0.40)
        r2 = parse_and_validate("max_drawdown_pct", "0.40")
        assert r2.ok and r2.value == pytest.approx(0.40)
        r3 = parse_and_validate("max_drawdown_pct", "40")
        assert r3.ok and r3.value == pytest.approx(0.40)

    def test_reject_bad_types(self):
        assert parse_and_validate("min_active_days", "abc").ok is False
        assert parse_and_validate("alert_decisions", "EVERYTHING").ok is False


class TestMigrationAndDefaults:
    def test_default_balanced_includes_max_event_age(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "m.db"),
            copy_max_event_age_seconds=600,
        )
        initialize_database(settings)
        user = get_user_settings(settings, 42)
        assert user.profile == "Balanced"
        assert user.max_event_age_seconds == 600

    def test_missing_age_key_migrates_from_global(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "mig.db"),
            copy_max_event_age_seconds=777,
        )
        initialize_database(settings)
        copy_storage.save_copy_user_settings(
            settings,
            telegram_id=7,
            payload={
                "profile": "Balanced",
                "min_resolved_markets": 20,
                "min_active_days": 30,
                "max_drawdown_pct": 0.40,
                "max_single_win_contribution": 0.40,
                "max_category_concentration": 0.45,
                "min_liquidity_usd": 500.0,
                "poll_interval_seconds": 120,
                "alert_decisions": "FOLLOW_AND_WATCH",
            },
        )
        user = get_user_settings(settings, 7)
        assert user.max_event_age_seconds == 777

    def test_failed_validation_does_not_mutate(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "v.db"))
        initialize_database(settings)
        set_profile(settings, 1, "Balanced")
        before = settings_to_payload(get_user_settings(settings, 1))
        ok, error, preview = preview_setting_change(
            settings, 1, "poll_interval_seconds", 5
        )
        assert ok is False
        assert preview is None
        assert error
        after = settings_to_payload(get_user_settings(settings, 1))
        assert after == before


class TestConfirmTokens:
    def test_confirm_save_atomic(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "c.db"))
        initialize_database(settings)
        ok, _, preview = preview_setting_change(
            settings, 9, "min_resolved_markets", 40
        )
        assert ok and preview
        conf = create_edit_confirmation(
            settings,
            operator_id=9,
            proposed_payload=preview["proposed_payload"],
            fingerprint=preview["fingerprint"],
            changed_keys=["min_resolved_markets"],
            preview_lines=["preview"],
        )
        status, user = execute_confirmed_settings(
            settings, token=conf.token, operator_id=9
        )
        assert status == "ok"
        assert user.min_resolved_markets == 40
        status2, _ = execute_confirmed_settings(
            settings, token=conf.token, operator_id=9
        )
        assert status2 == "invalid_confirmation"

    def test_stale_fingerprint(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "s.db"))
        initialize_database(settings)
        ok, _, preview = preview_setting_change(
            settings, 3, "min_active_days", 60
        )
        assert ok and preview
        conf = create_edit_confirmation(
            settings,
            operator_id=3,
            proposed_payload=preview["proposed_payload"],
            fingerprint=preview["fingerprint"],
            changed_keys=["min_active_days"],
            preview_lines=[],
        )
        update_setting(settings, 3, "min_resolved_markets", 40)
        status, _ = execute_confirmed_settings(
            settings, token=conf.token, operator_id=3
        )
        assert status == "stale_preview"

    def test_operator_mismatch(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "o.db"))
        initialize_database(settings)
        ok, _, preview = preview_setting_change(
            settings, 1, "poll_interval_seconds", 180
        )
        assert ok and preview
        conf = create_edit_confirmation(
            settings,
            operator_id=1,
            proposed_payload=preview["proposed_payload"],
            fingerprint=preview["fingerprint"],
            changed_keys=["poll_interval_seconds"],
            preview_lines=[],
        )
        status, _ = execute_confirmed_settings(
            settings, token=conf.token, operator_id=2
        )
        assert status == "operator_mismatch"

    def test_expired_token(self, tmp_path, monkeypatch):
        settings = make_test_settings(database_path=str(tmp_path / "e.db"))
        initialize_database(settings)
        ok, _, preview = preview_setting_change(
            settings, 1, "max_event_age_seconds", 300
        )
        assert ok and preview
        conf = create_edit_confirmation(
            settings,
            operator_id=1,
            proposed_payload=preview["proposed_payload"],
            fingerprint=preview["fingerprint"],
            changed_keys=["max_event_age_seconds"],
            preview_lines=[],
        )
        monkeypatch.setattr(
            "app.copy_intel.settings_confirm.time.time",
            lambda: conf.expires_at + 1,
        )
        status, _ = execute_confirmed_settings(
            settings, token=conf.token, operator_id=1
        )
        assert status == "confirmation_expired"

    def test_reset_to_balanced(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "r.db"))
        initialize_database(settings)
        set_profile(settings, 5, "Exploratory")
        conf, lines = create_reset_confirmation(settings, operator_id=5)
        assert "Balanced" in "\n".join(lines)
        status, user = execute_confirmed_settings(
            settings, token=conf.token, operator_id=5
        )
        assert status == "ok"
        assert user.profile == "Balanced"
        assert user.min_resolved_markets == 20


class TestPollSkip:
    def test_skip_within_user_interval(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "p.db"),
            copy_poll_interval_seconds=60,
        )
        initialize_database(settings)
        wallet = add_wallet(
            settings,
            "0xee00ba338c59557141789b127927a55f5cc5cea1",
            added_by=11,
        )
        update_setting(settings, 11, "poll_interval_seconds", 600)
        copy_storage.touch_wallet_last_polled(settings, wallet)
        assert should_skip_wallet_poll(settings, wallet, owner_id=11) is True

    def test_no_skip_when_interval_elapsed(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "p2.db"))
        initialize_database(settings)
        wallet = add_wallet(
            settings,
            "0xee00ba338c59557141789b127927a55f5cc5cea1",
            added_by=12,
        )
        update_setting(settings, 12, "poll_interval_seconds", 60)
        past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        with copy_storage.get_connection(settings) as conn:
            conn.execute(
                "UPDATE wallet_monitor_state SET last_polled_at = ? WHERE wallet = ?",
                (past, wallet),
            )
        assert should_skip_wallet_poll(settings, wallet, owner_id=12) is False

    def test_missing_owner_uses_balanced_default(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "p3.db"))
        initialize_database(settings)
        wallet = "0xee00ba338c59557141789b127927a55f5cc5cea1"
        copy_storage.add_watchlist_wallet(settings, wallet, added_by=None)
        from app.copy_intel.freshness import begin_subscription

        begin_subscription(settings, wallet)
        copy_storage.touch_wallet_last_polled(settings, wallet)
        assert should_skip_wallet_poll(settings, wallet, owner_id=None) is True


class TestMaxEventAge:
    def test_hard_filter_uses_max_admin_age(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "a.db"),
            admin_ids=[1, 2],
            copy_max_event_age_seconds=600,
        )
        initialize_database(settings)
        update_setting(settings, 1, "max_event_age_seconds", 300)
        update_setting(settings, 2, "max_event_age_seconds", 900)
        assert hard_filter_max_event_age_seconds(settings) == 900

    def test_filter_alertable_override(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "f.db"))
        initialize_database(settings)
        wallet = add_wallet(
            settings,
            "0xee00ba338c59557141789b127927a55f5cc5cea1",
            added_by=1,
        )
        from app.copy_intel.freshness import begin_subscription

        begin_subscription(settings, wallet)
        now = int(time.time())
        with copy_storage.get_connection(settings) as conn:
            conn.execute(
                """
                UPDATE wallet_monitor_state
                SET baseline_status = 'COMPLETE',
                    baseline_version = 2,
                    newest_event_ts = ?,
                    newest_event_uid = 'seed',
                    subscription_started_at = ?
                WHERE wallet = ?
                """,
                (now - 10_000, datetime.fromtimestamp(now - 20_000, tz=timezone.utc).isoformat(), wallet),
            )
        event = TradeEvent(
            wallet=wallet,
            event_type="open",
            condition_id="c1",
            asset="a1",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=10,
            tx_hash="0xabc",
            ts=now - 400,
            title="t",
            slug="s",
            dedup_key="d1",
        )
        out = filter_alertable_events(
            settings, wallet, [event], now=now, max_age_seconds=300
        )
        assert out == []
        event2 = TradeEvent(
            wallet=wallet,
            event_type="open",
            condition_id="c2",
            asset="a2",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=10,
            tx_hash="0xdef",
            ts=now - 400,
            title="t2",
            slug="s2",
            dedup_key="d2",
        )
        out2 = filter_alertable_events(
            settings, wallet, [event2], now=now, max_age_seconds=600
        )
        assert len(out2) == 1

    def test_delivery_soft_filters(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "d.db"))
        initialize_database(settings)
        update_setting(settings, 1, "max_event_age_seconds", 300)
        update_setting(settings, 1, "alert_decisions", "FOLLOW_ONLY")
        now = int(time.time())
        assert _event_too_old_for_subscriber(settings, 1, now - 400, now=now) is True
        assert _event_too_old_for_subscriber(settings, 1, now - 100, now=now) is False
        rec = MagicMock(action="WATCH")
        assert _subscriber_allows_recommendation(settings, 1, rec) is False
        rec2 = MagicMock(action="COPY")
        assert _subscriber_allows_recommendation(settings, 1, rec2) is True


class TestFormatting:
    def test_settings_card_includes_age(self):
        from app.telegram.copy_intel.formatting import format_settings_card

        user = build_settings(1, "Balanced")
        text = format_settings_card(user, modified=True)
        assert "Max event age" in text
        assert "Modified" in text
        assert "TELEGRAM_TOKEN" not in text
