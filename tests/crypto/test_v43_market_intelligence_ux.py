"""V4.3 Market Intelligence UX: menus, Filters, History, honesty, isolation."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.crypto.opportunity.formatting import (
    format_crypto_opportunity_card,
    format_opportunity_history,
    format_top_crypto_opportunities,
)
from app.crypto.opportunity.models import PaperNetOpportunity, ScanReport
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    CryptoOpportunitySubscription,
    balanced_crypto_settings,
)
from app.crypto.opportunity.settings_edit import (
    apply_custom_text,
    apply_field_set,
    cancel_confirmation,
    clear_confirmations_for_tests,
    create_confirmation,
    execute_confirmation,
    load_settings,
    presets_for_field,
)
from app.crypto.exchanges import build_clients, build_v43_clients
from app.crypto.models import EXCHANGE_IDS
from app.crypto.venue_registry import V43_EXCHANGE_IDS
from app.database import crypto_opportunity_storage as storage
from app.database.storage import initialize_database, upsert_user
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.inline_menus import (
    CB_CRYPTO_ARB,
    CB_CRYPTO_FILTERS,
    CB_CRYPTO_HISTORY,
    CB_CRYPTO_MENU,
    CB_HEALTH,
    CB_MAIN,
    CB_MOI_MENU,
    CB_PM_MENU,
    CB_RESEARCH_MENU,
    CB_SETTINGS,
    crypto_inline_keyboard,
    main_inline_keyboard,
)
from datetime import datetime, timezone
from tests.conftest import make_test_settings
from tests.test_telegram_commands import _context


def _labels(markup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _datas(markup) -> list[str]:
    return [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]


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


def test_guest_and_admin_main_menu():
    guest = _labels(main_inline_keyboard(is_admin=False))
    assert guest == [
        "📈 Polymarket",
        "📊 Market Intelligence",
        "⚙️ Settings",
    ]
    assert "📚 Market Research" not in guest
    assert "📊 Market Opportunities" not in guest
    assert "🩺 System Health" not in guest

    admin = _labels(main_inline_keyboard(is_admin=True))
    assert "📊 Market Intelligence" in admin
    assert "🩺 System Health" in admin
    assert "📚 Market Research" not in admin
    assert "📊 Market Opportunities" not in admin
    datas = _datas(main_inline_keyboard(is_admin=True))
    assert CB_HEALTH in datas
    assert CB_RESEARCH_MENU not in datas
    assert CB_MOI_MENU not in datas
    assert CB_CRYPTO_MENU in datas
    assert CB_PM_MENU in datas
    assert CB_SETTINGS in datas


def test_market_intelligence_menu_exact_buttons():
    labels = _labels(crypto_inline_keyboard())
    assert labels == [
        "📈 New Opportunities",
        "🎛 Filters",
        "🕘 Opportunity History",
        "↩️ Back",
    ]
    datas = _datas(crypto_inline_keyboard())
    assert datas == [CB_CRYPTO_ARB, CB_CRYPTO_FILTERS, CB_CRYPTO_HISTORY, CB_MAIN]
    assert "crypto:overview" not in datas
    assert "crypto:spread" not in datas
    assert "crypto:exchanges" not in datas


def test_callback_data_length_limit():
    for field in ("ex", "pa", "np", "roi", "sl", "dep", "age", "cd"):
        for value in presets_for_field(field):
            cb = f"crypto:filters:set:{field}:{value}"
            assert len(cb.encode("utf-8")) <= 64, cb
    for field in ("pa", "np", "roi", "sl", "dep", "age", "cd"):
        cb = f"crypto:filters:custom:{field}"
        assert len(cb.encode("utf-8")) <= 64, cb
    token = "a" * 16
    for kind in ("confirm", "cancel"):
        cb = f"crypto:filters:{kind}:{token}"
        assert len(cb.encode("utf-8")) <= 64, cb


def test_filters_preview_confirm_cancel_persistence_isolation(tmp_path):
    clear_confirmations_for_tests()
    settings = make_test_settings(database_path=str(tmp_path / "filt.db"))
    initialize_database(settings)
    upsert_user(settings, 11, "a", "A")
    upsert_user(settings, 22, "b", "B")

    a0 = load_settings(settings, 11)
    assert a0.delivery_enabled is False
    proposed = apply_field_set(a0, field="pa", value="250")
    conf = create_confirmation(
        telegram_id=11,
        starting=a0,
        proposed=proposed,
        changed_keys=["pa"],
    )
    assert "Confirm" in "\n".join(conf.preview_lines)

    # Cancel writes nothing.
    assert cancel_confirmation(telegram_id=11, token=conf.token) == "cancelled"
    assert load_settings(settings, 11).paper_allocation_usd == a0.paper_allocation_usd

    conf2 = create_confirmation(
        telegram_id=11,
        starting=load_settings(settings, 11),
        proposed=apply_field_set(load_settings(settings, 11), field="pa", value="250"),
        changed_keys=["pa"],
    )
    status, saved = execute_confirmation(
        settings, telegram_id=11, token=conf2.token
    )
    assert status == "ok"
    assert saved is not None
    assert saved.paper_allocation_usd == Decimal("250")
    assert saved.delivery_enabled is False
    assert load_settings(settings, 11).paper_allocation_usd == Decimal("250")
    # User B unchanged / isolated defaults.
    assert load_settings(settings, 22).paper_allocation_usd == Decimal("100")


def test_filters_bybit_rejected_and_stale_token(tmp_path):
    clear_confirmations_for_tests()
    settings = make_test_settings(database_path=str(tmp_path / "bybit.db"))
    initialize_database(settings)
    upsert_user(settings, 5, "u", "U")
    current = load_settings(settings, 5)
    with pytest.raises(ValueError):
        apply_field_set(current, field="ex", value="bybit")

    conf = create_confirmation(
        telegram_id=5,
        starting=current,
        proposed=apply_field_set(current, field="cd", value="3600"),
        changed_keys=["cd"],
    )
    # Stale: persist a newer revision first.
    from dataclasses import replace

    newer = replace(current, revision=current.revision + 1, delivery_enabled=False)
    storage.save_subscription(
        settings,
        CryptoOpportunitySubscription(telegram_id=5, settings=newer),
    )
    status, saved = execute_confirmation(settings, telegram_id=5, token=conf.token)
    assert status == "stale_preview"
    assert saved is None


def test_delivery_remains_disabled_on_confirm(tmp_path):
    clear_confirmations_for_tests()
    settings = make_test_settings(
        database_path=str(tmp_path / "deloff.db"),
        crypto_opportunity_delivery_enabled=False,
    )
    initialize_database(settings)
    upsert_user(settings, 9, "u", "U")
    current = load_settings(settings, 9)
    from dataclasses import replace

    # Attempt to sneak delivery_enabled True into proposed object.
    sneaky = replace(current, delivery_enabled=True, cooldown_seconds=3600)
    conf = create_confirmation(
        telegram_id=9,
        starting=current,
        proposed=sneaky,
        changed_keys=["cd"],
    )
    assert conf.proposed.delivery_enabled is False
    status, saved = execute_confirmation(settings, telegram_id=9, token=conf.token)
    assert status == "ok"
    assert saved is not None
    assert saved.delivery_enabled is False


def test_history_rows_empty_and_no_cross_user(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "hist.db"))
    initialize_database(settings)
    upsert_user(settings, 1, "a", "A")
    upsert_user(settings, 2, "b", "B")
    empty = format_opportunity_history(deliveries=[], last_sent=[])
    assert "No history yet" in empty

    storage.insert_delivery(
        settings,
        telegram_id=1,
        opportunity_uid="crypto_opp_a",
        opportunity_key="crypto_v1:BTC/USDT:binance:okx:spot:buy_low_sell_high",
        payload_text="card",
        status="SENT",
    )
    storage.record_sent(
        settings,
        telegram_id=1,
        opportunity_key="crypto_v1:BTC/USDT:binance:okx:spot:buy_low_sell_high",
        opportunity_uid="crypto_opp_a",
    )
    d1 = storage.list_deliveries_for_user(settings, telegram_id=1)
    d2 = storage.list_deliveries_for_user(settings, telegram_id=2)
    assert len(d1) == 1
    assert d2 == []
    text = format_opportunity_history(
        deliveries=d1,
        last_sent=storage.list_last_sent_for_user(settings, telegram_id=1),
    )
    assert "SENT" in text
    assert "{" not in text


def test_honest_spot_arb_card_no_forbidden_claims():
    opp = PaperNetOpportunity(
        opportunity_key="k",
        opportunity_uid="u",
        pair="BTC/USDT",
        product_type="spot",
        buy_venue="binance",
        sell_venue="okx",
        buy_ask=100.0,
        sell_bid=101.0,
        buy_vwap=100.1,
        sell_vwap=100.9,
        gross_spread_usd=0.8,
        buy_fee_usd=0.1,
        sell_fee_usd=0.1,
        trading_fees_usd=0.2,
        estimated_slippage=0.01,
        paper_allocation_usd=Decimal("100"),
        executable_base=1.0,
        executable_liquidity_usd=500.0,
        net_profit_usd=0.5,
        roi=0.005,
        signal_age_seconds=1.0,
        checked_at=datetime.now(timezone.utc),
    )
    text = format_crypto_opportunity_card(opp)
    assert "spot cross-exchange paper arbitrage" in text.lower()
    assert "binance" in text.lower()
    assert "okx" in text.lower()
    assert "BUY venue:" in text
    assert "SELL venue:" in text
    # Must not claim these capabilities as available.
    claim_phrases = [
        "open long",
        "open short",
        "take-profit",
        "stop-loss",
        "perpetual",
        "dated future",
        "funding rate",
        "basis trade",
        "stock signal",
        "forex",
        "news-based",
    ]
    lower = text.lower()
    for phrase in claim_phrases:
        assert phrase not in lower
    assert "not long/short" in lower or "not LONG/SHORT" in text

    disabled = format_top_crypto_opportunities(
        ScanReport(enabled=False, opportunities=(), rejects=())
    )
    assert "Scanner disabled" in disabled
    assert "spot cross-exchange" in disabled.lower()


def test_bybit_v3_preserved_v43_excluded():
    settings = make_test_settings()
    assert "bybit" in EXCHANGE_IDS
    assert "bybit" in build_clients(settings)
    assert "bybit" not in build_v43_clients(settings)
    assert set(build_v43_clients(settings)) == set(V43_EXCHANGE_IDS)


def test_moi_wti_backend_still_importable():
    from app.market_opportunity.sources import eia  # noqa: F401
    from app.telegram.market_opportunity.handlers import handle_moi_callback  # noqa: F401


@pytest.mark.asyncio
async def test_filters_and_history_callbacks(tmp_path):
    clear_confirmations_for_tests()
    settings = make_test_settings(database_path=str(tmp_path / "ux.db"))
    initialize_database(settings)
    upsert_user(settings, 42, "u", "U")
    context = _context()
    context.application.bot_data = {"settings": settings}

    update = _callback_update(CB_CRYPTO_FILTERS, user_id=42)
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Filters" in text
    assert "Spot" in text
    assert "delivery stays off" in text.lower()

    update = _callback_update(CB_CRYPTO_HISTORY, user_id=42)
    await inline_callback_handler(update, context)
    hist = update.callback_query.message.edit_text.await_args.args[0]
    assert "Opportunity History" in hist
    assert "No history yet" in hist


def test_custom_integer_and_decimal_input_preview_confirm_cancel(tmp_path):
    clear_confirmations_for_tests()
    settings = make_test_settings(
        database_path=str(tmp_path / "custom.db"),
        crypto_opportunity_scanner_enabled=False,
        crypto_opportunity_delivery_enabled=False,
    )
    initialize_database(settings)
    upsert_user(settings, 77, "c", "C")
    upsert_user(settings, 88, "d", "D")
    assert settings.crypto_opportunity_scanner_enabled is False

    current = load_settings(settings, 77)
    # Integer custom: cooldown within existing bounds.
    proposed_int = apply_custom_text(current, field="cd", text="2700")
    conf = create_confirmation(
        telegram_id=77,
        starting=current,
        proposed=proposed_int,
        changed_keys=["cd"],
    )
    preview = "\n".join(conf.preview_lines)
    assert "→" in preview
    assert "2700" in preview
    assert cancel_confirmation(telegram_id=77, token=conf.token) == "cancelled"
    assert load_settings(settings, 77).cooldown_seconds == current.cooldown_seconds

    # Decimal custom where supported: min net profit.
    proposed_dec = apply_custom_text(current, field="np", text="2.5")
    conf2 = create_confirmation(
        telegram_id=77,
        starting=current,
        proposed=proposed_dec,
        changed_keys=["np"],
    )
    assert "2.5" in "\n".join(conf2.preview_lines)
    status, saved = execute_confirmation(settings, telegram_id=77, token=conf2.token)
    assert status == "ok"
    assert saved is not None
    assert saved.minimum_net_profit_usd == Decimal("2.5")
    assert saved.delivery_enabled is False
    assert load_settings(settings, 88).minimum_net_profit_usd == Decimal("0")


def test_custom_input_rejects_invalid_negative_and_out_of_range(tmp_path):
    clear_confirmations_for_tests()
    settings = make_test_settings(database_path=str(tmp_path / "badcustom.db"))
    initialize_database(settings)
    upsert_user(settings, 3, "u", "U")
    current = load_settings(settings, 3)
    before = load_settings(settings, 3)

    with pytest.raises(ValueError):
        apply_custom_text(current, field="np", text="not-a-number")
    with pytest.raises(ValueError):
        apply_custom_text(current, field="np", text="-1")
    with pytest.raises(ValueError):
        apply_custom_text(current, field="age", text="30.5")
    with pytest.raises(ValueError):
        apply_custom_text(current, field="sl", text="0.9")  # above MAXIMUM_SLIPPAGE_BOUNDS
    with pytest.raises(ValueError):
        apply_custom_text(current, field="cd", text="10")  # below COOLDOWN_SECONDS_BOUNDS
    with pytest.raises(ValueError):
        apply_custom_text(current, field="pa", text="75")  # not an approved allocation preset

    assert load_settings(settings, 3) == before


@pytest.mark.asyncio
async def test_custom_value_callback_and_text_router(tmp_path):
    from app.telegram.crypto.handlers import (
        _FILTERS_EDIT_KEY,
        crypto_filters_text_input,
    )
    from app.telegram.menu import _settings_text_router

    clear_confirmations_for_tests()
    settings = make_test_settings(database_path=str(tmp_path / "router.db"))
    initialize_database(settings)
    upsert_user(settings, 55, "u", "U")
    context = _context()
    context.application.bot_data = {"settings": settings}
    context.user_data = {}

    update = _callback_update("crypto:filters:custom:roi", user_id=55)
    await inline_callback_handler(update, context)
    assert context.user_data.get(_FILTERS_EDIT_KEY) == "roi"
    prompt = update.callback_query.message.edit_text.await_args.args[0]
    assert "custom" in prompt.lower() or "Enter custom" in prompt or "ROI" in prompt

    # Unrelated user has no pending field — text must not be treated as filter input.
    other_ctx = _context()
    other_ctx.application.bot_data = {"settings": settings}
    other_ctx.user_data = {}
    other_update = MagicMock()
    other_update.effective_user = MagicMock(id=99, username="x", first_name="X")
    other_update.effective_message = AsyncMock()
    other_update.effective_message.text = "0.02"
    other_update.message = other_update.effective_message
    other_update.callback_query = None
    assert await crypto_filters_text_input(other_update, other_ctx) is False

    # Valid custom text for pending user → preview, no persist yet.
    msg_update = MagicMock()
    msg_update.effective_user = MagicMock(id=55, username="u", first_name="U")
    msg_update.effective_message = AsyncMock()
    msg_update.effective_message.text = "0.015"
    msg_update.message = msg_update.effective_message
    msg_update.callback_query = None
    context.user_data[_FILTERS_EDIT_KEY] = "roi"
    await _settings_text_router(msg_update, context)
    assert context.user_data.get(_FILTERS_EDIT_KEY) is None
    preview = msg_update.effective_message.reply_text.await_args.args[0]
    assert "→" in preview
    assert "0.015" in preview
    assert load_settings(settings, 55).minimum_roi == Decimal("0")
