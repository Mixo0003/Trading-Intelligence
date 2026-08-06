"""Admin-only Market Opportunities Telegram handlers (moi:*)."""

from __future__ import annotations

import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.telegram.auth import require_admin
from app.telegram.inline_menus import (
    CB_MOI_EVIDENCE,
    CB_MOI_FILTERS,
    CB_MOI_GOLD,
    CB_MOI_HISTORY,
    CB_MOI_MENU,
    CB_MOI_RISKS,
    CB_MOI_STATS,
    CB_MOI_TOP,
    CB_MOI_WTI,
    moi_hub_keyboard,
    moi_submenu_keyboard,
)
from app.telegram.market_opportunity.formatting import (
    DETAIL_NOT_FOUND_TEXT,
    EVIDENCE_EMPTY_TEXT,
    FILTERS_PLACEHOLDER_TEXT,
    GOLD_STUB_TEXT,
    HISTORY_PLACEHOLDER_TEXT,
    HUB_TEXT,
    RISKS_EMPTY_TEXT,
    STATS_PLACEHOLDER_TEXT,
    TOP_EMPTY_TEXT,
    WTI_EMPTY_TEXT,
    format_evidence,
    format_opportunity_detail,
    format_opportunity_list,
    format_risks,
)
from app.telegram.nav import edit_or_reply

logger = logging.getLogger(__name__)

MOI_CALLBACKS = {
    CB_MOI_MENU,
    CB_MOI_TOP,
    CB_MOI_WTI,
    CB_MOI_GOLD,
    CB_MOI_EVIDENCE,
    CB_MOI_RISKS,
    CB_MOI_HISTORY,
    CB_MOI_STATS,
    CB_MOI_FILTERS,
}


def is_moi_callback(data: str) -> bool:
    return data.startswith("moi:")


def _list_published(settings: Settings, *, symbol: str | None = None):
    from app.database import market_opportunity_storage as moi_storage

    return moi_storage.list_opportunities(
        settings,
        status="published",
        symbol=symbol,
        limit=20,
    )


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
        rows = _list_published(settings)
        text = format_opportunity_list(
            "📊 Top Opportunities", rows, empty_text=TOP_EMPTY_TEXT
        )
        await edit_or_reply(update, text, reply_markup=moi_submenu_keyboard())
        return
    if data == CB_MOI_WTI:
        rows = _list_published(settings, symbol="WTI_CUSHING")
        text = format_opportunity_list(
            "🛢 WTI Opportunities", rows, empty_text=WTI_EMPTY_TEXT
        )
        await edit_or_reply(update, text, reply_markup=moi_submenu_keyboard())
        return
    if data == CB_MOI_GOLD:
        await edit_or_reply(
            update, GOLD_STUB_TEXT, reply_markup=moi_submenu_keyboard()
        )
        return
    if data == CB_MOI_EVIDENCE or data.startswith("moi:evidence:"):
        uid = data.split(":", 2)[2] if data.startswith("moi:evidence:") else ""
        if not uid:
            await edit_or_reply(
                update, EVIDENCE_EMPTY_TEXT, reply_markup=moi_submenu_keyboard()
            )
            return
        from app.database import market_opportunity_storage as moi_storage

        row = moi_storage.get_opportunity(settings, uid)
        evidence = (row or {}).get("evidence") or []
        await edit_or_reply(
            update,
            format_evidence(evidence),
            reply_markup=moi_submenu_keyboard(),
        )
        return
    if data == CB_MOI_RISKS or data.startswith("moi:risks:"):
        uid = data.split(":", 2)[2] if data.startswith("moi:risks:") else ""
        if not uid:
            await edit_or_reply(
                update, RISKS_EMPTY_TEXT, reply_markup=moi_submenu_keyboard()
            )
            return
        from app.database import market_opportunity_storage as moi_storage

        row = moi_storage.get_opportunity(settings, uid)
        risks = list((row or {}).get("counterarguments") or [])
        if not risks:
            raw = (row or {}).get("counterarguments_json") or "[]"
            try:
                risks = json.loads(raw) if isinstance(raw, str) else list(raw)
            except json.JSONDecodeError:
                risks = []
        await edit_or_reply(
            update, format_risks(risks), reply_markup=moi_submenu_keyboard()
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
        uid = ""
        if ":" in data[len("moi:detail") :]:
            uid = data.split(":", 2)[2]
        if not uid:
            await edit_or_reply(
                update, DETAIL_NOT_FOUND_TEXT, reply_markup=moi_submenu_keyboard()
            )
            return
        from app.database import market_opportunity_storage as moi_storage

        row = moi_storage.get_opportunity(settings, uid)
        await edit_or_reply(
            update,
            format_opportunity_detail(row),
            reply_markup=moi_submenu_keyboard(),
        )
        return

    logger.info("Unknown moi callback %s", data)
    await edit_or_reply(update, HUB_TEXT, reply_markup=moi_hub_keyboard())
