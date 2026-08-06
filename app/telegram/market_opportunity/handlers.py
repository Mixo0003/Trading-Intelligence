"""Admin-only Market Opportunities Telegram handlers (moi:*)."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.telegram.auth import require_admin
from app.telegram.inline_menus import (
    CB_MOI_EVIDENCE,
    CB_MOI_FILTERS,
    CB_MOI_HISTORY,
    CB_MOI_MENU,
    CB_MOI_RISKS,
    CB_MOI_STATS,
    CB_MOI_TOP,
    moi_hub_keyboard,
    moi_submenu_keyboard,
)
from app.telegram.market_opportunity.formatting import (
    DETAIL_NOT_FOUND_TEXT,
    EVIDENCE_EMPTY_TEXT,
    FILTERS_PLACEHOLDER_TEXT,
    HISTORY_PLACEHOLDER_TEXT,
    HUB_TEXT,
    RISKS_EMPTY_TEXT,
    STATS_PLACEHOLDER_TEXT,
    TOP_EMPTY_TEXT,
)
from app.telegram.nav import edit_or_reply

logger = logging.getLogger(__name__)

MOI_CALLBACKS = {
    CB_MOI_MENU,
    CB_MOI_TOP,
    CB_MOI_EVIDENCE,
    CB_MOI_RISKS,
    CB_MOI_HISTORY,
    CB_MOI_STATS,
    CB_MOI_FILTERS,
}


def is_moi_callback(data: str) -> bool:
    return data.startswith("moi:")


async def handle_moi_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await require_admin(update, settings, action=f"cb:{data}"):
        return

    if data == CB_MOI_MENU:
        await edit_or_reply(update, HUB_TEXT, reply_markup=moi_hub_keyboard())
        return
    if data == CB_MOI_TOP:
        await edit_or_reply(
            update, TOP_EMPTY_TEXT, reply_markup=moi_submenu_keyboard()
        )
        return
    if data == CB_MOI_EVIDENCE or data.startswith("moi:evidence:"):
        await edit_or_reply(
            update, EVIDENCE_EMPTY_TEXT, reply_markup=moi_submenu_keyboard()
        )
        return
    if data == CB_MOI_RISKS or data.startswith("moi:risks:"):
        await edit_or_reply(
            update, RISKS_EMPTY_TEXT, reply_markup=moi_submenu_keyboard()
        )
        return
    if data == CB_MOI_HISTORY:
        await edit_or_reply(
            update, HISTORY_PLACEHOLDER_TEXT, reply_markup=moi_submenu_keyboard()
        )
        return
    if data == CB_MOI_STATS:
        await edit_or_reply(
            update, STATS_PLACEHOLDER_TEXT, reply_markup=moi_submenu_keyboard()
        )
        return
    if data == CB_MOI_FILTERS:
        await edit_or_reply(
            update, FILTERS_PLACEHOLDER_TEXT, reply_markup=moi_submenu_keyboard()
        )
        return
    if data.startswith("moi:detail"):
        await edit_or_reply(
            update, DETAIL_NOT_FOUND_TEXT, reply_markup=moi_submenu_keyboard()
        )
        return

    logger.info("Unknown moi callback %s", data)
    await edit_or_reply(update, HUB_TEXT, reply_markup=moi_hub_keyboard())
