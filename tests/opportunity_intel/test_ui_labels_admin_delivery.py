"""Live Mode ON/OFF control, scanner arming, and admin-only delivery."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.opportunity_storage import (
    initialize_opportunity_schema,
    load_delivery,
    load_subscription,
    save_delivery_gates,
    save_subscription,
)
from app.database.storage import initialize_database, upsert_user
from app.opportunity_intel.delivery import (
    recheck_delivery_authorization,
    select_delivery_wave,
)
from app.opportunity_intel.delivery_gates import OpportunityDeliveryGates
from app.opportunity_intel.display_labels import (
    ALERT_MODE_AUTO_LABEL,
    ALERT_MODE_MANUAL_LABEL,
    LIVE_MODE_OFF_LABEL,
    LIVE_MODE_ON_LABEL,
    alert_mode_label,
    live_mode_label,
    parse_alert_mode_text,
    parse_live_mode_text,
)
from app.opportunity_intel.models import (
    AlertMode,
    LiveMode,
    ObservationMode,
    OpportunityDiagnosticCode,
)
from app.opportunity_intel.scanner_control import scanner_armed
from app.opportunity_intel.settings import balanced_settings
from app.opportunity_intel.settings_edit import (
    apply_field_value,
    apply_live_mode_immediate,
    preview_field_change,
)
from app.opportunity_intel.status_view import build_scanner_status_view
from app.telegram.auth import is_admin
from app.telegram.inline_menus import (
    CB_OPP,
    CB_OPP_ALERTS,
    CB_OPP_LIVE,
    CB_OPP_LIVE_OFF,
    CB_OPP_LIVE_ON,
    CB_OPP_STATUS,
    opportunity_alert_mode_keyboard,
    opportunity_live_mode_keyboard,
)
from app.telegram.opportunity_intel.formatting import (
    format_alert_settings_card,
    format_filters_card,
    format_live_mode_screen,
    format_opportunity_card,
)
from app.telegram.opportunity_intel.handlers import handle_opportunity_callback
from tests.conftest import make_test_settings
from tests.opportunity_intel.test_delivery_wave import (
    ADMIN_ID,
    USER_ID,
    _instant_sub,
    _seed_signal_and_metrics,
)

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opp_ui_labels.db"),
        admin_ids=[ADMIN_ID],
        opportunity_scanner_enabled=False,
        opportunity_auto_alerts_enabled=False,
        opportunity_user_delivery_enabled=False,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    upsert_user(value, ADMIN_ID, "admin", "Admin")
    upsert_user(value, USER_ID, "user", "User")
    return value


@pytest.fixture
def settings_no_admin(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opp_ui_labels_no_admin.db"),
        admin_ids=[],
        opportunity_scanner_enabled=False,
        opportunity_auto_alerts_enabled=False,
        opportunity_user_delivery_enabled=False,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    upsert_user(value, ADMIN_ID, "admin", "Admin")
    return value


def _callback_update(user_id: int = ADMIN_ID):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=user_id, username="u", first_name="T")
    update.message = None
    return update


def _context(settings):
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.application.job_queue = MagicMock()
    context.application.job_queue.get_jobs_by_name = MagicMock(return_value=[])
    context.user_data = {}
    context.bot = MagicMock()
    return context


async def _emit(settings, data: str, *, user_id: int = ADMIN_ID, context=None) -> tuple[str, object]:
    update = _callback_update(user_id)
    ctx = context or _context(settings)
    await handle_opportunity_callback(update, ctx, data)
    message = update.effective_message
    call = message.edit_text.await_args or message.reply_text.await_args
    assert call is not None
    return call.args[0], call.kwargs.get("reply_markup")


def _button_texts(keyboard) -> list[str]:
    return [
        button.text
        for row in keyboard.inline_keyboard
        for button in row
    ]


def test_live_mode_display_labels_off_and_on():
    assert live_mode_label(LiveMode.OFF) == LIVE_MODE_OFF_LABEL
    assert live_mode_label(LiveMode.STRICT) == LIVE_MODE_ON_LABEL
    assert parse_live_mode_text("ON") == LiveMode.STRICT
    assert parse_live_mode_text("STRICT") == LiveMode.STRICT
    assert parse_live_mode_text("OFF") == LiveMode.OFF


def test_legacy_strict_and_rest_do_not_break_settings():
    current = balanced_settings()
    via_on = apply_field_value(current, field_key="live_mode", raw_value="ON")
    via_strict = apply_field_value(current, field_key="live_mode", raw_value="STRICT")
    assert via_on.live_mode == LiveMode.STRICT
    assert via_strict.live_mode == LiveMode.STRICT
    with pytest.raises(ValueError):
        apply_field_value(current, field_key="live_mode", raw_value="REST")


def test_alert_mode_display_labels_manual_and_auto():
    assert alert_mode_label(AlertMode.ON_DEMAND_ONLY) == ALERT_MODE_MANUAL_LABEL
    assert alert_mode_label(AlertMode.INSTANT) == ALERT_MODE_AUTO_LABEL
    assert parse_alert_mode_text("MANUAL") == AlertMode.ON_DEMAND_ONLY
    assert parse_alert_mode_text("AUTO") == AlertMode.INSTANT


def test_live_mode_keyboard_only_on_off_back():
    keyboard = opportunity_live_mode_keyboard()
    texts = _button_texts(keyboard)
    assert texts == ["ON", "OFF", "⬅️ Back"]
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }
    assert callbacks == {CB_OPP_LIVE_ON, CB_OPP_LIVE_OFF, CB_OPP}


def test_preview_and_alert_buttons_hide_internal_enum_names():
    current = replace(
        balanced_settings(),
        live_mode=LiveMode.STRICT,
        alert_mode=AlertMode.INSTANT,
    )
    _, live_lines = preview_field_change(current, field_key="live_mode", raw_value="OFF")
    _, alert_lines = preview_field_change(current, field_key="alert_mode", raw_value="MANUAL")
    joined = "\n".join(live_lines + alert_lines)
    assert "STRICT" not in joined
    assert "ON_DEMAND_ONLY" not in joined
    assert "INSTANT" not in joined
    alert_buttons = set(_button_texts(opportunity_alert_mode_keyboard()))
    assert alert_buttons >= {"MANUAL", "AUTO"}
    assert "ON_DEMAND_ONLY" not in alert_buttons
    assert "INSTANT" not in alert_buttons


def test_filters_and_alert_cards_use_display_labels():
    scanner = replace(
        balanced_settings(),
        live_mode=LiveMode.STRICT,
        alert_mode=AlertMode.INSTANT,
    )
    filters = format_filters_card(scanner)
    alerts = format_alert_settings_card(
        scanner,
        user_delivery_available=False,
        admin_delivery_available=True,
        is_admin_viewer=True,
        auto_alerts_enabled=True,
        scanner_enabled=True,
        worker_label="Ready",
    )
    assert "Live mode: ON" in filters
    assert "Alert mode: AUTO" in alerts
    assert "STRICT" not in filters
    assert "ON_DEMAND_ONLY" not in alerts
    assert "INSTANT" not in alerts


def test_live_mode_screen_is_status_only():
    text = format_live_mode_screen(live_mode=LiveMode.STRICT)
    assert text == "Live Mode\nCurrent status: ON"
    assert "STRICT" not in text
    assert "REST" not in text
    assert "scanner" not in text.lower()
    assert "disclaimer" not in text.lower()
    assert "Top Opportunities" not in text


@pytest.mark.asyncio
async def test_live_mode_opens_with_only_on_off_back(settings):
    text, keyboard = await _emit(settings, CB_OPP_LIVE)
    assert text == "Live Mode\nCurrent status: OFF"
    assert _button_texts(keyboard) == ["ON", "OFF", "⬅️ Back"]
    lowered = text.lower()
    assert "strict" not in lowered
    assert "rest" not in lowered
    assert "on_demand" not in lowered
    assert "instant" not in lowered
    assert "configures filtering" not in lowered


@pytest.mark.asyncio
async def test_live_mode_on_callback_saves_and_refreshes(settings):
    context = _context(settings)
    text, _ = await _emit(settings, CB_OPP_LIVE_ON, context=context)
    assert text == "Live Mode\nCurrent status: ON"
    sub = load_subscription(settings, ADMIN_ID)
    assert sub is not None
    assert sub.settings.live_mode == LiveMode.STRICT
    assert scanner_armed(settings) is True
    view = build_scanner_status_view(settings, viewer_live_mode=LiveMode.STRICT)
    assert view.scanner_enabled is True
    assert "Scanner: enabled" in view.text or "Scanner worker:" in view.text
    assert "Scanner: disabled" not in view.text


@pytest.mark.asyncio
async def test_live_mode_off_callback_saves_and_disarms(settings):
    apply_live_mode_immediate(
        settings, telegram_id=ADMIN_ID, live_mode=LiveMode.STRICT
    )
    assert scanner_armed(settings) is True
    text, _ = await _emit(settings, CB_OPP_LIVE_OFF)
    assert text == "Live Mode\nCurrent status: OFF"
    sub = load_subscription(settings, ADMIN_ID)
    assert sub is not None
    assert sub.settings.live_mode == LiveMode.OFF
    assert scanner_armed(settings) is False
    view = build_scanner_status_view(settings, viewer_live_mode=LiveMode.OFF)
    assert view.scanner_enabled is False
    assert "Scanner: disabled" in view.text
    assert "Scanner worker: stopped" in view.text


def test_live_mode_and_scanner_status_same_source(settings):
    assert scanner_armed(settings) is False
    off_view = build_scanner_status_view(settings, viewer_live_mode=LiveMode.OFF)
    assert off_view.scanner_enabled is False
    apply_live_mode_immediate(
        settings, telegram_id=ADMIN_ID, live_mode=LiveMode.STRICT
    )
    assert scanner_armed(settings) is True
    on_view = build_scanner_status_view(settings, viewer_live_mode=LiveMode.STRICT)
    assert on_view.scanner_enabled is True
    assert live_mode_label(LiveMode.STRICT) in on_view.text or "Live Mode: ON" in on_view.text


def test_live_mode_off_blocks_new_alert_delivery(settings):
    apply_live_mode_immediate(
        settings, telegram_id=ADMIN_ID, live_mode=LiveMode.STRICT
    )
    save_subscription(
        settings,
        _instant_sub(ADMIN_ID, live_mode=LiveMode.OFF, revision=2),
        expected_revision=1,
    )
    signal = _seed_signal_and_metrics(settings)
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=False),
    )
    result = select_delivery_wave(
        settings, opportunity_uid=signal.opportunity_uid, now=NOW
    )
    assert result.pending == ()


def test_live_mode_on_allows_configured_admin_delivery(settings):
    apply_live_mode_immediate(
        settings, telegram_id=ADMIN_ID, live_mode=LiveMode.STRICT
    )
    save_subscription(
        settings,
        _instant_sub(ADMIN_ID, live_mode=LiveMode.STRICT, revision=2),
        expected_revision=1,
    )
    signal = _seed_signal_and_metrics(settings)
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=False),
    )
    result = select_delivery_wave(
        settings, opportunity_uid=signal.opportunity_uid, now=NOW
    )
    assert len(result.pending) == 1
    assert result.pending[0].telegram_id == ADMIN_ID
    ok, reason = recheck_delivery_authorization(
        settings,
        telegram_id=ADMIN_ID,
        opportunity_uid=signal.opportunity_uid,
        settings_revision=2,
        now=NOW,
    )
    assert ok is True
    assert reason is None


def test_regular_users_do_not_receive_admin_only_alerts(settings):
    apply_live_mode_immediate(
        settings, telegram_id=ADMIN_ID, live_mode=LiveMode.STRICT
    )
    save_subscription(
        settings,
        _instant_sub(ADMIN_ID, live_mode=LiveMode.STRICT, revision=2),
        expected_revision=1,
    )
    save_subscription(
        settings,
        _instant_sub(USER_ID, live_mode=LiveMode.STRICT),
        expected_revision=None,
    )
    signal = _seed_signal_and_metrics(settings)
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=False),
    )
    result = select_delivery_wave(
        settings, opportunity_uid=signal.opportunity_uid, now=NOW
    )
    assert [row.telegram_id for row in result.pending] == [ADMIN_ID]
    assert is_admin(settings, USER_ID) is False


def test_missing_admin_configuration_fails_safe(settings_no_admin):
    assert is_admin(settings_no_admin, ADMIN_ID) is False
    apply_live_mode_immediate(
        settings_no_admin, telegram_id=ADMIN_ID, live_mode=LiveMode.STRICT
    )
    assert scanner_armed(settings_no_admin) is False
    view = build_scanner_status_view(settings_no_admin)
    assert view.admin_delivery_available is False
    signal = _seed_signal_and_metrics(settings_no_admin)
    save_subscription(
        settings_no_admin,
        _instant_sub(ADMIN_ID, live_mode=LiveMode.STRICT, revision=2),
        expected_revision=1,
    )
    save_delivery_gates(
        settings_no_admin,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=False),
    )
    result = select_delivery_wave(
        settings_no_admin, opportunity_uid=signal.opportunity_uid, now=NOW
    )
    assert result.pending == ()
    suppressed = load_delivery(settings_no_admin, ADMIN_ID, signal.opportunity_uid)
    assert suppressed is not None
    assert suppressed.last_error_code == OpportunityDiagnosticCode.USER_GATE_DISABLED


def test_opportunity_card_hides_strict_enum_name():
    from decimal import Decimal
    from datetime import timedelta

    from app.opportunity_intel.models import (
        MovementDirection,
        OpportunityReasonCode,
        SignalFamily,
    )
    from app.opportunity_intel.payload import build_delivery_card_payload

    now = NOW
    payload = build_delivery_card_payload(
        opportunity_uid="opp_live",
        signal_at=now,
        wave_at=now,
        signal_family=SignalFamily.EXECUTABLE_MOMENTUM,
        question="Live game?",
        outcome="Yes",
        event_slug="market-one",
        mode=ObservationMode.LIVE_STRICT,
        canonical_score=Decimal("88"),
        reason_codes=(OpportunityReasonCode.LIVE_MARKET,),
        direction=MovementDirection.UP,
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.05"),
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal("0.02"),
        relative_spread=Decimal("0.03"),
        allocation_usd=Decimal("100"),
        execution_vwap=Decimal("0.56"),
        slippage=Decimal("0.01"),
        executable_ask_depth_usd=Decimal("1000"),
        volume_24h_usd=Decimal("250000"),
        liquidity_usd=Decimal("100000"),
        end_at=now + timedelta(hours=2),
        source_observed_at=now,
    )
    text = format_opportunity_card(payload, now=now)
    assert "LIVE / IN-PLAY" in text
    assert "EXTRA SAFETY CHECKS" in text
    assert "STRICT" not in text


@pytest.mark.asyncio
async def test_telegram_menu_routing_intact(settings):
    text, keyboard = await _emit(settings, CB_OPP_LIVE)
    assert text.startswith("Live Mode")
    assert _button_texts(keyboard) == ["ON", "OFF", "⬅️ Back"]
    alert_text, _ = await _emit(settings, CB_OPP_ALERTS)
    assert alert_text.startswith("Opportunity Alert Settings")
    assert "ON_DEMAND_ONLY" not in alert_text
    assert "INSTANT" not in alert_text
    status_text, _ = await _emit(settings, CB_OPP_STATUS)
    assert status_text.startswith("Scanner Status")
