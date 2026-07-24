"""Telegram handlers for Market Opportunity Scanner (cached data only)."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.database import opportunity_storage
from app.opportunity_intel.catalog import load_top_opportunities
from app.opportunity_intel.categories import KNOWN_CATEGORY_IDS, KNOWN_CATEGORY_SET
from app.opportunity_intel.settings import ALLOCATION_PRESETS_USD
from app.opportunity_intel.settings_edit import (
    cancel_confirmation,
    create_edit_confirmation,
    create_reset_confirmation,
    drop_operator_confirmations,
    execute_confirmed_settings,
    get_or_default_subscription,
    preview_field_change,
    resolve_field_key,
    toggle_category_draft,
)
from app.opportunity_intel.status_view import build_scanner_status_view
from app.telegram.inline_menus import (
    CB_OPP,
    CB_OPP_ALERTS,
    CB_OPP_BACK,
    CB_OPP_FILTERS,
    CB_OPP_HISTORY,
    CB_OPP_LIVE,
    CB_OPP_STATUS,
    CB_OPP_TOP,
    CB_OSET_ALERT,
    CB_OSET_CANCEL,
    CB_OSET_CAT,
    CB_OSET_CONFIRM,
    CB_OSET_EDIT,
    CB_OSET_LIVE,
    CB_OSET_RESET,
    CB_OSET_RESET_NO,
    CB_OSET_RESET_OK,
    OPP_MENU_TEXT,
    opportunity_alert_mode_keyboard,
    opportunity_allocation_keyboard,
    opportunity_category_keyboard,
    opportunity_confirm_keyboard,
    opportunity_inline_keyboard,
    opportunity_live_mode_keyboard,
    opportunity_reset_confirm_keyboard,
    opportunity_settings_edit_keyboard,
    opportunity_settings_keyboard,
)
from app.telegram.nav import edit_or_reply, open_main_menu
from app.telegram.opportunity_intel.formatting import (
    format_live_markets_help,
    format_opportunity_card,
    format_settings_card,
    format_signal_history,
)

logger = logging.getLogger(__name__)

_SETTINGS_TOKEN_KEY = "opp_settings_confirm_token"
_SETTINGS_EDIT_KEY = "opp_settings_edit_field"
_CATEGORY_DRAFT_KEY = "opp_category_draft"
_CATEGORY_ALL_KEY = "opp_category_all"


def _settings(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["settings"]


async def _offload(work):
    return await asyncio.to_thread(work)


async def opportunities_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await edit_or_reply(update, OPP_MENU_TEXT, reply_markup=opportunity_inline_keyboard())


async def show_opportunity_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await edit_or_reply(update, OPP_MENU_TEXT, reply_markup=opportunity_inline_keyboard())


async def show_top_opportunities(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return
    await edit_or_reply(
        update,
        "Loading cached opportunities…",
        reply_markup=opportunity_inline_keyboard(),
    )
    telegram_id = int(user.id)

    def work():
        return load_top_opportunities(settings, telegram_id=telegram_id)

    result = await _offload(work)
    if result.status != "ready":
        await edit_or_reply(
            update, result.message, reply_markup=opportunity_inline_keyboard()
        )
        return
    blocks = [result.message, ""]
    for entry in result.entries:
        blocks.append(format_opportunity_card(entry.payload))
        blocks.append("")
    text = "\n".join(blocks).rstrip()
    await edit_or_reply(update, text, reply_markup=opportunity_inline_keyboard())


async def show_alert_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return

    def work():
        sub = get_or_default_subscription(settings, int(user.id))
        gates = opportunity_storage.load_delivery_gates(settings)
        return format_settings_card(
            sub.settings,
            user_delivery_available=gates.user_delivery_enabled,
        )

    text = await _offload(work)
    await edit_or_reply(update, text, reply_markup=opportunity_settings_keyboard())


async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_alert_settings(update, context)


async def show_live_markets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await edit_or_reply(
        update,
        format_live_markets_help(),
        reply_markup=opportunity_inline_keyboard(),
    )


async def show_signal_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    await edit_or_reply(
        update, "Loading signal history…", reply_markup=opportunity_inline_keyboard()
    )

    def work():
        signals = opportunity_storage.list_recent_signals(settings, limit=8)
        markets: dict[str, tuple[str, str]] = {}
        for signal in signals:
            state = opportunity_storage.load_market_state(settings, signal.asset_id)
            if state is not None:
                markets[signal.asset_id] = (state.question, state.outcome)
        return format_signal_history(signals, markets)

    text = await _offload(work)
    await edit_or_reply(update, text, reply_markup=opportunity_inline_keyboard())


async def show_scanner_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    await edit_or_reply(
        update, "Loading scanner status…", reply_markup=opportunity_inline_keyboard()
    )
    view = await _offload(lambda: build_scanner_status_view(settings))
    await edit_or_reply(update, view.text, reply_markup=opportunity_inline_keyboard())


async def handle_opportunity_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    if data in {CB_OPP, CB_OPP_BACK}:
        await show_opportunity_menu(update, context)
        return
    if data == CB_OPP_TOP:
        await show_top_opportunities(update, context)
        return
    if data in {CB_OPP_ALERTS, CB_OPP_FILTERS}:
        await show_alert_settings(update, context)
        return
    if data == CB_OPP_LIVE:
        await show_live_markets(update, context)
        return
    if data == CB_OPP_HISTORY:
        await show_signal_history(update, context)
        return
    if data == CB_OPP_STATUS:
        await show_scanner_status(update, context)
        return
    if data.startswith("oset:") or data in {
        CB_OSET_EDIT,
        CB_OSET_RESET,
        CB_OSET_CONFIRM,
        CB_OSET_CANCEL,
        CB_OSET_RESET_OK,
        CB_OSET_RESET_NO,
        CB_OSET_CAT,
        CB_OSET_LIVE,
        CB_OSET_ALERT,
    }:
        await handle_opportunity_settings_flow(update, context, data)
        return
    logger.info("Unhandled opportunity callback: %s", data)


async def handle_opportunity_settings_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return
    operator_id = int(user.id)

    if data == CB_OSET_EDIT:
        context.user_data[_SETTINGS_EDIT_KEY] = None
        await edit_or_reply(
            update,
            "Edit Opportunity Settings\nChoose a field:",
            reply_markup=opportunity_settings_edit_keyboard(),
        )
        return

    if data == CB_OSET_RESET:
        confirmation = await _offload(
            lambda: create_reset_confirmation(settings, operator_id=operator_id)
        )
        context.user_data[_SETTINGS_TOKEN_KEY] = confirmation.token
        await edit_or_reply(
            update,
            "\n".join(confirmation.preview_lines),
            reply_markup=opportunity_reset_confirm_keyboard(),
        )
        return

    if data in {CB_OSET_CONFIRM, CB_OSET_RESET_OK}:
        token = context.user_data.get(_SETTINGS_TOKEN_KEY)
        if not token:
            await edit_or_reply(
                update,
                "No pending confirmation.",
                reply_markup=opportunity_settings_keyboard(),
            )
            return

        def work():
            return execute_confirmed_settings(
                settings, token=str(token), operator_id=operator_id
            )

        status, _sub = await _offload(work)
        context.user_data[_SETTINGS_TOKEN_KEY] = None
        context.user_data[_SETTINGS_EDIT_KEY] = None
        if status != "ok":
            await edit_or_reply(
                update,
                f"Could not save settings ({status}).",
                reply_markup=opportunity_settings_keyboard(),
            )
            return
        await show_alert_settings(update, context)
        return

    if data in {CB_OSET_CANCEL, CB_OSET_RESET_NO}:
        token = context.user_data.get(_SETTINGS_TOKEN_KEY)
        if token:
            await _offload(
                lambda: cancel_confirmation(
                    settings, token=str(token), operator_id=operator_id
                )
            )
        context.user_data[_SETTINGS_TOKEN_KEY] = None
        context.user_data[_SETTINGS_EDIT_KEY] = None
        drop_operator_confirmations(operator_id)
        await show_alert_settings(update, context)
        return

    if data == CB_OSET_LIVE:
        await edit_or_reply(
            update,
            "Choose live mode:",
            reply_markup=opportunity_live_mode_keyboard(),
        )
        return

    if data == CB_OSET_ALERT:
        await edit_or_reply(
            update,
            "Choose alert mode:\n"
            "Personal alert mode is separate from global user-delivery availability.",
            reply_markup=opportunity_alert_mode_keyboard(),
        )
        return

    if data == CB_OSET_CAT:
        sub = await _offload(lambda: get_or_default_subscription(settings, operator_id))
        cats = sub.settings.categories
        context.user_data[_CATEGORY_ALL_KEY] = cats.all_categories
        context.user_data[_CATEGORY_DRAFT_KEY] = (
            set(KNOWN_CATEGORY_IDS)
            if cats.all_categories
            else set(cats.category_ids)
        )
        await edit_or_reply(
            update,
            "Category filters\n"
            "Empty selection is not allowed — choose Select all or at least one category.",
            reply_markup=opportunity_category_keyboard(
                frozenset(context.user_data[_CATEGORY_DRAFT_KEY]),
                all_categories=bool(context.user_data[_CATEGORY_ALL_KEY]),
            ),
        )
        return

    if data == "oset:cta":
        context.user_data[_CATEGORY_ALL_KEY] = True
        context.user_data[_CATEGORY_DRAFT_KEY] = set(KNOWN_CATEGORY_IDS)
        await edit_or_reply(
            update,
            "All known categories selected.",
            reply_markup=opportunity_category_keyboard(
                frozenset(KNOWN_CATEGORY_IDS), all_categories=True
            ),
        )
        return

    if data == "oset:ctc":
        context.user_data[_CATEGORY_ALL_KEY] = False
        context.user_data[_CATEGORY_DRAFT_KEY] = set()
        await edit_or_reply(
            update,
            "Cleared. Select at least one category before preview save.",
            reply_markup=opportunity_category_keyboard(frozenset(), all_categories=False),
        )
        return

    if data.startswith("oset:ct:") and data != "oset:ctp":
        category_id = data.removeprefix("oset:ct:")
        if category_id not in KNOWN_CATEGORY_SET:
            await edit_or_reply(
                update,
                "Unknown category.",
                reply_markup=opportunity_settings_edit_keyboard(),
            )
            return
        draft = set(context.user_data.get(_CATEGORY_DRAFT_KEY) or set())
        all_mode = bool(context.user_data.get(_CATEGORY_ALL_KEY))
        if all_mode:
            draft = set(KNOWN_CATEGORY_IDS)
            all_mode = False
        enabled = category_id not in draft
        draft = toggle_category_draft(draft, category_id, enable=enabled)
        context.user_data[_CATEGORY_ALL_KEY] = False
        context.user_data[_CATEGORY_DRAFT_KEY] = draft
        await edit_or_reply(
            update,
            "Category filters updated.",
            reply_markup=opportunity_category_keyboard(
                frozenset(draft), all_categories=False
            ),
        )
        return

    if data == "oset:ctp":
        all_mode = bool(context.user_data.get(_CATEGORY_ALL_KEY))
        draft = set(context.user_data.get(_CATEGORY_DRAFT_KEY) or set())
        if not all_mode and not draft:
            await edit_or_reply(
                update,
                "Empty category allowlist is not allowed. Select all or at least one.",
                reply_markup=opportunity_category_keyboard(frozenset(), all_categories=False),
            )
            return
        raw = "all" if all_mode else ",".join(sorted(draft))
        await _begin_preview(update, context, "categories", raw)
        return

    if data.startswith("oset:f:"):
        code = data.removeprefix("oset:f:")
        key = resolve_field_key(code)
        if key is None:
            return
        if key == "paper_allocation_usd":
            await edit_or_reply(
                update,
                "Choose paper allocation preset:",
                reply_markup=opportunity_allocation_keyboard(),
            )
            return
        context.user_data[_SETTINGS_EDIT_KEY] = key
        await edit_or_reply(
            update,
            f"Send a new value for {key}.\n"
            "Quiet hours format: HH:MM-HH:MM or off",
            reply_markup=opportunity_settings_edit_keyboard(),
        )
        return

    if data.startswith("oset:pv:"):
        parts = data.split(":")
        if len(parts) != 4:
            return
        _, _, code, idx_s = parts
        key = resolve_field_key(code)
        if key is None:
            return
        idx = int(idx_s)
        if key == "paper_allocation_usd":
            value = str(ALLOCATION_PRESETS_USD[idx])
        elif key == "live_mode":
            value = ("OFF", "STRICT")[idx]
        elif key == "alert_mode":
            value = ("ON_DEMAND_ONLY", "INSTANT")[idx]
        else:
            return
        await _begin_preview(update, context, key, value)
        return


async def _begin_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    field_key: str,
    raw_value: str,
) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return
    operator_id = int(user.id)

    def work():
        current = get_or_default_subscription(settings, operator_id).settings
        proposed, lines = preview_field_change(
            current, field_key=field_key, raw_value=raw_value
        )
        confirmation = create_edit_confirmation(
            settings,
            operator_id=operator_id,
            starting=current,
            proposed=proposed,
            changed_keys=[field_key],
            preview_lines=lines,
        )
        return confirmation

    try:
        confirmation = await _offload(work)
    except ValueError as exc:
        await edit_or_reply(
            update,
            f"Invalid value: {exc}",
            reply_markup=opportunity_settings_edit_keyboard(),
        )
        return
    context.user_data[_SETTINGS_TOKEN_KEY] = confirmation.token
    context.user_data[_SETTINGS_EDIT_KEY] = field_key
    await edit_or_reply(
        update,
        "\n".join(confirmation.preview_lines),
        reply_markup=opportunity_confirm_keyboard(),
    )


async def opportunity_settings_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Return True when an opportunity settings edit consumed the message."""
    key = context.user_data.get(_SETTINGS_EDIT_KEY)
    if not key:
        return False
    message = update.effective_message
    if message is None or not message.text:
        return False
    await _begin_preview(update, context, str(key), message.text)
    return True
