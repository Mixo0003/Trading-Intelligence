"""V2.2.4 product hub navigation: Polymarket / Crypto / Research shells."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import CallbackQueryHandler

from app.telegram.auth import ACCESS_DENIED
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.inline_menus import (
    CB_COPY,
    CB_CRYPTO_ARB,
    CB_CRYPTO_MENU,
    CB_MAIN,
    CB_OPP,
    CB_OPP_HISTORY,
    CB_OPP_TOP,
    CB_PM_MENU,
    CB_PM_SETTINGS,
    CB_RESEARCH_COIN,
    CB_RESEARCH_MENU,
    CB_SETTINGS,
    CB_TOP,
    CB_WEATHER,
    CB_WX_TOP,
    crypto_inline_keyboard,
    main_inline_keyboard,
    polymarket_inline_keyboard,
    polymarket_settings_keyboard,
    research_inline_keyboard,
    weather_inline_keyboard,
)
from app.telegram.menu import build_telegram_application
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


class TestProductHubKeyboards:
    def test_top_level_hub_labels(self):
        labels = _labels(main_inline_keyboard(is_admin=False))
        assert labels == [
            "📈 Polymarket",
            "₿ Crypto Intelligence",
            "📚 Market Research",
            "⚙️ Settings",
        ]
        assert "🩺 System Health" not in labels

    def test_health_admin_only_on_main(self):
        guest = _labels(main_inline_keyboard(is_admin=False))
        admin = _labels(main_inline_keyboard(is_admin=True))
        assert "🩺 System Health" not in guest
        assert "🩺 System Health" in admin
        assert admin.index("🩺 System Health") < admin.index("⚙️ Settings")

    def test_polymarket_submenu_routes(self):
        datas = _datas(polymarket_inline_keyboard())
        assert datas == [
            CB_WEATHER,
            CB_COPY,
            CB_OPP,
            CB_OPP_TOP,
            CB_OPP_HISTORY,
            CB_PM_SETTINGS,
            CB_MAIN,
        ]
        assert CB_TOP not in datas
        assert CB_WX_TOP not in datas

    def test_crypto_and_research_stub_menus(self):
        crypto_labels = _labels(crypto_inline_keyboard())
        research_labels = _labels(research_inline_keyboard())
        assert crypto_labels == [
            "📊 Market Overview",
            "💱 Arbitrage Opportunities",
            "↔️ Spread Monitor",
            "🏦 Exchanges & Health",
            "↩️ Back",
        ]
        assert research_labels == [
            "🪙 Coin Analysis",
            "🛢 Commodity Analysis",
            "📈 Price History & Trends",
            "📰 News & Market Drivers",
            "🔎 Compare Assets",
            "↩️ Back",
        ]
        crypto = _datas(crypto_inline_keyboard())
        research = _datas(research_inline_keyboard())
        assert all(d.startswith("crypto:") or d == CB_MAIN for d in crypto)
        assert all(d.startswith("research:") or d == CB_MAIN for d in research)
        assert "crypto:overview" in crypto
        assert "crypto:arb" in crypto
        assert "research:coin" in research
        assert "research:compare" in research
        assert CB_MAIN in crypto
        assert CB_MAIN in research

    def test_namespace_isolation(self):
        leaf_prefixes = ("wx:", "copy:", "disc:", "cset:", "opp:", "oset:", "health:")
        main_datas = _datas(main_inline_keyboard(is_admin=True))
        assert all(
            d.startswith("pm:")
            or d.startswith("crypto:")
            or d.startswith("research:")
            or d.startswith("nav:")
            for d in main_datas
        )
        pm_datas = _datas(polymarket_inline_keyboard())
        # Polymarket hub deep-links into existing leaf namespaces.
        assert any(d.startswith("nav:") for d in pm_datas)
        assert any(d.startswith("opp:") for d in pm_datas)
        assert all(not d.startswith("wx:") for d in pm_datas)
        # Weather keeps wx: actions; only hub back uses pm:menu.
        weather_datas = _datas(weather_inline_keyboard())
        assert any(d.startswith("wx:") for d in weather_datas)
        assert CB_PM_MENU in weather_datas
        assert all(
            d.startswith("wx:") or d == CB_PM_MENU for d in weather_datas
        )
        # Crypto/Research stubs must not reuse Polymarket leaf prefixes.
        for prefix in leaf_prefixes:
            assert not any(
                d.startswith(prefix) for d in _datas(crypto_inline_keyboard()) if d != CB_MAIN
            )
            assert not any(
                d.startswith(prefix)
                for d in _datas(research_inline_keyboard())
                if d != CB_MAIN
            )

    def test_polymarket_settings_hub_links_only(self):
        datas = _datas(polymarket_settings_keyboard())
        assert "copy:settings" in datas
        assert "opp:alerts" in datas
        assert "opp:filters" in datas
        assert CB_PM_MENU in datas


@pytest.mark.asyncio
async def test_pm_menu_and_back_path():
    update = _callback_update(CB_PM_MENU)
    context = _context()
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Polymarket" in text
    markup = update.callback_query.message.edit_text.await_args.kwargs["reply_markup"]
    assert CB_WEATHER in _datas(markup)

    update = _callback_update(CB_WEATHER)
    await inline_callback_handler(update, context)
    weather_markup = update.callback_query.message.edit_text.await_args.kwargs[
        "reply_markup"
    ]
    assert CB_PM_MENU in _datas(weather_markup)

    update = _callback_update(CB_PM_MENU)
    await inline_callback_handler(update, context)
    update = _callback_update(CB_MAIN)
    await inline_callback_handler(update, context)
    main_text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Choose a section" in main_text


@pytest.mark.asyncio
async def test_crypto_and_research_stubs_are_placeholders():
    update = _callback_update(CB_CRYPTO_MENU)
    context = _context()
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Crypto Intelligence" in text
    assert "placeholders" in text.lower() or "Spread Monitor" in text

    update = _callback_update(CB_CRYPTO_ARB)
    await inline_callback_handler(update, context)
    stub = update.callback_query.message.edit_text.await_args.args[0]
    assert "placeholder" in stub.lower() or "Coming in V3" in stub

    update = _callback_update(CB_RESEARCH_MENU)
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Coming in V3" in text

    update = _callback_update(CB_RESEARCH_COIN)
    await inline_callback_handler(update, context)
    stub = update.callback_query.message.edit_text.await_args.args[0]
    assert "Coming in V3" in stub


@pytest.mark.asyncio
async def test_pm_top_opportunities_uses_opp_top_not_weather_nav_top(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "pm_top.db"))
    update = _callback_update(CB_OPP_TOP)
    context = _context()
    context.application.bot_data = {"settings": settings}
    with patch(
        "app.telegram.opportunity_intel.handlers.show_top_opportunities",
        new=AsyncMock(),
    ) as show_top:
        await inline_callback_handler(update, context)
        show_top.assert_awaited_once()

    update = _callback_update(CB_TOP)
    with patch(
        "app.telegram.handlers.top_from_callback", new=AsyncMock()
    ) as weather_top:
        await inline_callback_handler(update, context)
        weather_top.assert_awaited_once()


@pytest.mark.asyncio
async def test_pm_signal_history_routes_to_opp_history(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "hist.db"))
    update = _callback_update(CB_OPP_HISTORY)
    context = _context()
    context.application.bot_data = {"settings": settings}
    with patch(
        "app.telegram.opportunity_intel.handlers.show_signal_history",
        new=AsyncMock(),
    ) as show_hist:
        await inline_callback_handler(update, context)
        show_hist.assert_awaited_once()


@pytest.mark.asyncio
async def test_copy_admin_gate_still_enforced_from_pm_hub(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "auth.db"),
        admin_ids=[1001],
    )
    update = _callback_update(CB_COPY, user_id=42)
    context = _context()
    context.application.bot_data = {"settings": settings}
    await inline_callback_handler(update, context)
    update.effective_message.reply_text.assert_awaited_with(ACCESS_DENIED)


@pytest.mark.asyncio
async def test_settings_still_opens_global_settings():
    update = _callback_update(CB_SETTINGS)
    context = _context()
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Settings" in text
    assert "ANALYSIS ONLY" in text


def test_single_callback_handler_still_registered(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "bot.db"),
        telegram_token="123456:ABCDEF",
    )
    with patch("app.telegram.menu.initialize_database"), patch(
        "app.telegram.menu.schedule_report_jobs"
    ), patch("app.telegram.menu.schedule_copy_intel_jobs"), patch(
        "app.telegram.menu.schedule_opportunity_jobs"
    ), patch("app.telegram.menu.schedule_health_jobs"):
        app = build_telegram_application(settings)
    callback_handlers = [
        h for h in app.handlers[0] if isinstance(h, CallbackQueryHandler)
    ]
    assert len(callback_handlers) == 1


def test_no_v3_crypto_package_imported():
    import importlib
    import sys

    # V2.2.4 must not create or depend on app.v3.crypto.
    assert "app.v3" not in sys.modules
    assert "app.v3.crypto" not in sys.modules
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.v3.crypto")
