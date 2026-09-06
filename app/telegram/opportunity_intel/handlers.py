"""Telegram handlers for Market Opportunity Scanner."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.database import opportunity_storage
from app.opportunity_intel.catalog import load_trade_candidates
from app.opportunity_intel.categories import KNOWN_CATEGORY_IDS, KNOWN_CATEGORY_SET
from app.opportunity_intel.settings import ALLOCATION_PRESETS_USD
from app.opportunity_intel.models import LiveMode
from app.opportunity_intel.scanner_control import scanner_armed
from app.opportunity_intel.scheduler import schedule_opportunity_jobs
from app.opportunity_intel.settings_edit import (
    ALERT_FIELD_KEYS,
    FILTER_FIELD_KEYS,
    apply_live_edge_immediate,
    apply_live_mode_immediate,
    cancel_confirmation,
    create_edit_confirmation,
    create_reset_confirmation,
    drop_operator_confirmations,
    execute_confirmed_settings,
    field_prompt,
    get_or_default_subscription,
    preview_field_change,
    resolve_field_key,
    toggle_category_draft,
)
from app.opportunity_intel.status_view import build_scanner_status_view
from app.telegram.auth import is_admin
from app.telegram.inline_menus import (
    CB_OPP,
    CB_OPP_ALERTS,
    CB_OPP_BACK,
    CB_OPP_FILTERS,
    CB_OPP_HISTORY,
    CB_OPP_LIVE,
    CB_OPP_LIVE_OFF,
    CB_OPP_LIVE_ON,
    CB_OPP_STATUS,
    CB_OPP_TOP,
    CB_OSET_ALERT,
    CB_OSET_CANCEL,
    CB_OSET_CAT,
    CB_OSET_CONFIRM,
    CB_OSET_EDIT,
    CB_OSET_LIVE,
    CB_OSET_LIVE_EDGE,
    CB_OSET_LIVE_EDGE_OFF,
    CB_OSET_LIVE_EDGE_ON,
    CB_OSET_RESET,
    CB_OSET_RESET_NO,
    CB_OSET_RESET_OK,
    OPP_MENU_TEXT,
    opportunity_alert_mode_keyboard,
    opportunity_allocation_keyboard,
    opportunity_category_keyboard,
    opportunity_confirm_keyboard,
    opportunity_filters_keyboard,
    opportunity_inline_keyboard,
    opportunity_live_edge_keyboard,
    opportunity_live_mode_keyboard,
    opportunity_reset_confirm_keyboard,
    opportunity_settings_keyboard,
)
from app.telegram.nav import edit_or_reply, open_main_menu
from app.telegram.opportunity_intel.formatting import (
    format_alert_settings_card,
    format_filters_card,
    format_live_edge_screen,
    format_live_mode_screen,
    format_trade_candidate_card,
    format_signal_history,
)

logger = logging.getLogger(__name__)

_SETTINGS_TOKEN_KEY = "opp_settings_confirm_token"
_SETTINGS_EDIT_KEY = "opp_settings_edit_field"
_CATEGORY_DRAFT_KEY = "opp_category_draft"
_CATEGORY_ALL_KEY = "opp_category_all"
_LIVE_BACK_KEY = "opp_live_back"


def _settings(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["settings"]


def _keyboard_for_field(field_key: str | None):
    """Keep each field's edit/back path inside the menu that owns it."""
    if field_key in FILTER_FIELD_KEYS:
        return opportunity_filters_keyboard()
    if field_key in ALERT_FIELD_KEYS:
        return opportunity_settings_keyboard()
    return opportunity_inline_keyboard()


async def _show_owning_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, field_key: str | None
) -> None:
    if field_key in FILTER_FIELD_KEYS:
        await show_filters(update, context)
        return
    await show_alert_settings(update, context)


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
        return load_trade_candidates(settings, telegram_id=telegram_id)

    result = await _offload(work)
    live_edge_blocks: list[str] = []
    sub = await _offload(lambda: get_or_default_subscription(settings, telegram_id))
    if sub.settings.live_edge_enabled:
        from app.opportunity_intel.live_edge.catalog import load_live_edge_cards

        cards = await _offload(
            lambda: load_live_edge_cards(
                settings, telegram_id=telegram_id, user_settings=sub.settings
            )
        )
        live_edge_blocks = [card.text for card in cards]
    if result.status != "ready" and not live_edge_blocks:
        await edit_or_reply(
            update, result.message, reply_markup=opportunity_inline_keyboard()
        )
        return
    blocks: list[str] = []
    if result.status == "ready":
        blocks.append(result.message)
        blocks.append("")
        for entry in result.entries:
            blocks.append(format_trade_candidate_card(entry.payload, entry.decision))
            blocks.append("")
    elif live_edge_blocks:
        blocks.append(result.message)
        blocks.append("")
    for text_block in live_edge_blocks:
        blocks.append(text_block)
        blocks.append("")
    text = "\n".join(blocks).rstrip() or result.message
    await edit_or_reply(update, text, reply_markup=opportunity_inline_keyboard())


async def show_alert_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return
    viewer_id = int(user.id)

    def work():
        sub = get_or_default_subscription(settings, viewer_id)
        view = build_scanner_status_view(
            settings, viewer_live_mode=sub.settings.live_mode
        )
        return format_alert_settings_card(
            sub.settings,
            user_delivery_available=view.user_delivery_globally_available,
            admin_delivery_available=view.admin_delivery_available,
            is_admin_viewer=is_admin(settings, viewer_id),
            auto_alerts_enabled=view.auto_alerts_enabled,
            scanner_enabled=view.scanner_enabled,
            worker_label=(
                "stopped" if not view.scanner_enabled else view.label
            ),
        )

    text = await _offload(work)
    await edit_or_reply(update, text, reply_markup=opportunity_settings_keyboard())


async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return

    def work():
        sub = get_or_default_subscription(settings, int(user.id))
        return format_filters_card(sub.settings)

    text = await _offload(work)
    await edit_or_reply(update, text, reply_markup=opportunity_filters_keyboard())


async def show_live_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    back_callback: str = CB_OPP,
) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return
    context.user_data[_LIVE_BACK_KEY] = back_callback

    def work():
        sub = get_or_default_subscription(settings, int(user.id))
        return format_live_mode_screen(live_mode=sub.settings.live_mode)

    text = await _offload(work)
    await edit_or_reply(
        update,
        text,
        reply_markup=opportunity_live_mode_keyboard(back_callback=back_callback),
    )


async def _set_live_mode_from_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    live_mode: LiveMode,
) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return
    operator_id = int(user.id)
    back_callback = str(context.user_data.get(_LIVE_BACK_KEY) or CB_OPP)

    def work():
        return apply_live_mode_immediate(
            settings, telegram_id=operator_id, live_mode=live_mode
        )

    sub = await _offload(work)
    # Admin Live Mode arms/disarms the shared scanner job at runtime.
    if is_admin(settings, operator_id):
        schedule_opportunity_jobs(context.application, _settings(context))
        if not scanner_armed(_settings(context)):
            _remove_opportunity_scan_jobs(context.application)
    text = format_live_mode_screen(live_mode=sub.settings.live_mode)
    await edit_or_reply(
        update,
        text,
        reply_markup=opportunity_live_mode_keyboard(back_callback=back_callback),
    )


async def show_live_edge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return

    def work():
        sub = get_or_default_subscription(settings, int(user.id))
        return format_live_edge_screen(enabled=sub.settings.live_edge_enabled)

    text = await _offload(work)
    await edit_or_reply(
        update,
        text,
        reply_markup=opportunity_live_edge_keyboard(),
    )


async def _set_live_edge_from_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    enabled: bool,
) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None:
        return
    operator_id = int(user.id)

    def work():
        return apply_live_edge_immediate(
            settings, telegram_id=operator_id, enabled=enabled
        )

    sub = await _offload(work)
    from app.opportunity_intel.live_edge.scheduler import schedule_live_edge_jobs

    schedule_live_edge_jobs(context.application, settings)
    text = format_live_edge_screen(enabled=sub.settings.live_edge_enabled)
    await edit_or_reply(
        update,
        text,
        reply_markup=opportunity_live_edge_keyboard(),
    )


def _remove_opportunity_scan_jobs(application) -> None:
    from app.opportunity_intel.scheduler import (
        OPPORTUNITY_SCAN_JOB_NAME,
        OPPORTUNITY_SCAN_REGISTERED_KEY,
    )

    job_queue = getattr(application, "job_queue", None)
    if job_queue is None:
        application.bot_data[OPPORTUNITY_SCAN_REGISTERED_KEY] = False
        return
    getter = getattr(job_queue, "get_jobs_by_name", None)
    if callable(getter):
        for job in getter(OPPORTUNITY_SCAN_JOB_NAME) or ():
            remover = getattr(job, "schedule_removal", None)
            if callable(remover):
                remover()
    application.bot_data[OPPORTUNITY_SCAN_REGISTERED_KEY] = False


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
        text = format_signal_history(signals, markets)
        user = update.effective_user
        if user is not None:
            from app.database import live_edge_storage
            from app.opportunity_intel.live_edge.formatting import (
                format_live_edge_history_block,
            )
            from app.opportunity_intel.settings_edit import get_or_default_subscription

            sub = get_or_default_subscription(settings, int(user.id))
            if sub.settings.live_edge_enabled:
                rows = live_edge_storage.list_history(
                    settings, telegram_id=int(user.id), limit=8
                )
                extra = format_live_edge_history_block(rows)
                if extra:
                    text = f"{text}\n\n{extra}"
        return text

    text = await _offload(work)
    await edit_or_reply(update, text, reply_markup=opportunity_inline_keyboard())


async def show_scanner_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    await edit_or_reply(
        update, "Loading scanner status…", reply_markup=opportunity_inline_keyboard()
    )

    def work():
        live_mode = None
        if user is not None:
            sub = get_or_default_subscription(settings, int(user.id))
            live_mode = sub.settings.live_mode
        return build_scanner_status_view(settings, viewer_live_mode=live_mode)

    view = await _offload(work)
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
    if data == CB_OPP_ALERTS:
        await show_alert_settings(update, context)
        return
    if data == CB_OPP_FILTERS:
        await show_filters(update, context)
        return
    if data == CB_OPP_LIVE:
        await show_live_mode(update, context, back_callback=CB_OPP)
        return
    if data == CB_OPP_LIVE_ON:
        await _set_live_mode_from_callback(
            update, context, live_mode=LiveMode.STRICT
        )
        return
    if data == CB_OPP_LIVE_OFF:
        await _set_live_mode_from_callback(
            update, context, live_mode=LiveMode.OFF
        )
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
        CB_OSET_LIVE_EDGE,
        CB_OSET_LIVE_EDGE_ON,
        CB_OSET_LIVE_EDGE_OFF,
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
        # Legacy combined edit entry point: alert-related fields.
        context.user_data[_SETTINGS_EDIT_KEY] = None
        await show_alert_settings(update, context)
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

        edited_field = context.user_data.get(_SETTINGS_EDIT_KEY)
        status, _sub = await _offload(work)
        context.user_data[_SETTINGS_TOKEN_KEY] = None
        context.user_data[_SETTINGS_EDIT_KEY] = None
        if status != "ok":
            await edit_or_reply(
                update,
                f"Could not save settings ({status}).",
                reply_markup=_keyboard_for_field(edited_field),
            )
            return
        await _show_owning_menu(update, context, edited_field)
        return

    if data in {CB_OSET_CANCEL, CB_OSET_RESET_NO}:
        token = context.user_data.get(_SETTINGS_TOKEN_KEY)
        edited_field = context.user_data.get(_SETTINGS_EDIT_KEY)
        if token:
            await _offload(
                lambda: cancel_confirmation(
                    settings, token=str(token), operator_id=operator_id
                )
            )
        context.user_data[_SETTINGS_TOKEN_KEY] = None
        context.user_data[_SETTINGS_EDIT_KEY] = None
        drop_operator_confirmations(operator_id)
        await _show_owning_menu(update, context, edited_field)
        return

    if data == CB_OSET_LIVE:
        await show_live_mode(update, context, back_callback=CB_OPP_FILTERS)
        return

    if data == CB_OSET_LIVE_EDGE:
        await show_live_edge(update, context)
        return
    if data == CB_OSET_LIVE_EDGE_ON:
        await _set_live_edge_from_callback(update, context, enabled=True)
        return
    if data == CB_OSET_LIVE_EDGE_OFF:
        await _set_live_edge_from_callback(update, context, enabled=False)
        return

    if data == CB_OSET_ALERT:
        await edit_or_reply(
            update,
            "\n".join(
                [
                    field_prompt("alert_mode"),
                    "",
                    "MANUAL: Alerts are shown only when you request them.",
                    "AUTO: Valid alerts are sent automatically.",
                    "Administrator delivery is separate from regular-user delivery.",
                ]
            ),
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
                reply_markup=opportunity_filters_keyboard(),
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
                field_prompt(key),
                reply_markup=opportunity_allocation_keyboard(),
            )
            return
        context.user_data[_SETTINGS_EDIT_KEY] = key
        await edit_or_reply(
            update,
            field_prompt(key),
            reply_markup=_keyboard_for_field(key),
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
            value = ("OFF", "ON")[idx]
        elif key == "alert_mode":
            value = ("MANUAL", "AUTO")[idx]
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
            f"Invalid value: {exc}\n\n{field_prompt(field_key)}",
            reply_markup=_keyboard_for_field(field_key),
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
