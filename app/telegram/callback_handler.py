"""Unified inline callback router for Weather + Copy Trading menus."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.region.session import set_user_scan_region
from app.region.types import ScanRegion
from app.telegram.inline_menus import (
    CB_COPY,
    CB_COPY_ALERTS,
    CB_COPY_DISCOVER,
    CB_COPY_PAPER,
    CB_COPY_SETTINGS,
    CB_COPY_STATUS,
    CB_COPY_WATCHLIST,
    CB_DISC_BACK,
    CB_DISC_CAT,
    CB_DISC_FIND,
    CB_DISC_NEXT,
    CB_DISC_PREV,
    CB_DISC_RISING,
    CB_DISC_RISK,
    CB_DISC_TOP,
    CB_MAIN,
    CB_SET_ALERT_BOTH,
    CB_SET_ALERT_FOLLOW,
    CB_SET_BALANCED,
    CB_SET_CONSERVATIVE,
    CB_SET_EXPLORATORY,
    CB_SETTINGS,
    CB_TOP,
    CB_WEATHER,
    CB_WX_FORECAST,
    CB_WX_SCAN_ALL,
    CB_WX_SCAN_ASIA,
    CB_WX_SCAN_EUROPE,
    CB_WX_SCAN_USA,
    CB_WX_TOP,
    COPY_MENU_TEXT,
    DISCOVERY_MENU_TEXT,
    SETTINGS_MENU_TEXT,
    WEATHER_MENU_TEXT,
    copy_inline_keyboard,
    copy_settings_inline_keyboard,
    discovery_inline_keyboard,
    weather_inline_keyboard,
)
from app.telegram.nav import answer_callback, edit_or_reply, open_main_menu

logger = logging.getLogger(__name__)

_SCAN_CB = {
    CB_WX_SCAN_USA: ScanRegion.USA,
    CB_WX_SCAN_EUROPE: ScanRegion.EUROPE,
    CB_WX_SCAN_ASIA: ScanRegion.ASIA,
    CB_WX_SCAN_ALL: ScanRegion.ALL,
}


async def inline_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    data = query.data
    await answer_callback(update)

    if data == CB_MAIN:
        await open_main_menu(update)
        return
    if data == CB_WEATHER:
        await edit_or_reply(update, WEATHER_MENU_TEXT, reply_markup=weather_inline_keyboard())
        return
    if data == CB_COPY:
        await edit_or_reply(update, COPY_MENU_TEXT, reply_markup=copy_inline_keyboard())
        return
    if data == CB_TOP:
        from app.telegram.handlers import top_from_callback

        await top_from_callback(update, context)
        return
    if data == CB_SETTINGS:
        await edit_or_reply(update, SETTINGS_MENU_TEXT, reply_markup=main_inline_keyboard())
        return

    if data in _SCAN_CB:
        from app.telegram.handlers import scan_from_callback

        set_user_scan_region(context, _SCAN_CB[data])
        await scan_from_callback(update, context)
        return
    if data == CB_WX_TOP:
        from app.telegram.handlers import top_signals_from_callback

        await top_signals_from_callback(update, context)
        return
    if data == CB_WX_FORECAST:
        from app.telegram.handlers import forecast_from_callback

        await forecast_from_callback(update, context)
        return

    if data == CB_COPY_DISCOVER:
        await edit_or_reply(update, DISCOVERY_MENU_TEXT, reply_markup=discovery_inline_keyboard())
        return
    if data == CB_COPY_WATCHLIST:
        from app.telegram.copy_intel.handlers import show_watchlist

        await show_watchlist(update, context, edit=True)
        return
    if data == CB_COPY_STATUS:
        from app.telegram.copy_intel.handlers import show_copy_status

        await show_copy_status(update, context, edit=True)
        return
    if data == CB_COPY_ALERTS:
        from app.telegram.copy_intel.handlers import show_copy_alerts

        await show_copy_alerts(update, context, edit=True)
        return
    if data == CB_COPY_PAPER:
        from app.telegram.copy_intel.handlers import show_paper

        await show_paper(update, context, edit=True)
        return
    if data == CB_COPY_SETTINGS:
        from app.telegram.copy_intel.handlers import show_copy_settings

        await show_copy_settings(update, context, edit=True)
        return

    if data in {CB_DISC_TOP, CB_DISC_FIND, CB_DISC_CAT, CB_DISC_RISING, CB_DISC_RISK}:
        from app.telegram.copy_intel.handlers import show_discovery_list

        mapping = {
            CB_DISC_TOP: ("Top Traders", {"decision": "FOLLOW"}),
            CB_DISC_FIND: ("Find Traders", {}),
            CB_DISC_CAT: ("Category Leaders", {}),
            CB_DISC_RISING: ("Rising Traders", {"rising_only": True}),
            CB_DISC_RISK: ("High Risk", {"high_risk_only": True}),
        }
        title, kwargs = mapping[data]
        await show_discovery_list(update, context, title=title, **kwargs)
        return
    if data in {CB_DISC_PREV, CB_DISC_NEXT, CB_DISC_BACK}:
        from app.telegram.copy_intel.handlers import discovery_nav

        await discovery_nav(update, context, data)
        return
    if data.startswith("disc:add:"):
        from app.telegram.copy_intel.handlers import add_watchlist_from_callback

        await add_watchlist_from_callback(update, context, data.removeprefix("disc:add:"))
        return

    if data in {
        CB_SET_CONSERVATIVE,
        CB_SET_BALANCED,
        CB_SET_EXPLORATORY,
        CB_SET_ALERT_FOLLOW,
        CB_SET_ALERT_BOTH,
    }:
        from app.telegram.copy_intel.handlers import apply_settings_callback

        await apply_settings_callback(update, context, data)
        return

    logger.info("Unhandled callback data: %s", data)
