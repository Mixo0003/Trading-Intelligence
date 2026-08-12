"""Telegram handlers for Market Intelligence (crypto:* callbacks)."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.crypto.health import probe_all_exchanges
from app.crypto.market_data import collect_spread_quotes
from app.crypto.opportunity.catalog import (
    ensure_default_profile,
    load_top_crypto_opportunities,
)
from app.crypto.opportunity.formatting import (
    format_opportunity_history,
    format_top_crypto_opportunities,
)
from app.crypto.opportunity.settings_edit import (
    FIELD_CODES,
    FIELD_LABELS,
    NUMERIC_FIELDS,
    apply_custom_text,
    apply_field_set,
    cancel_confirmation,
    create_confirmation,
    execute_confirmation,
    expected_format_hint,
    field_custom_prompt,
    format_filters_summary,
    load_settings,
    presets_for_field,
)
from app.crypto.overview import collect_market_overview
from app.crypto.risk import assert_crypto_read_only, crypto_feature_enabled
from app.database import crypto_opportunity_storage as storage
from app.telegram.crypto.formatting import (
    format_exchanges_health,
    format_market_overview,
    format_spread_monitor,
)
from app.telegram.inline_menus import (
    CB_CRYPTO_ARB,
    CB_CRYPTO_EXCHANGES,
    CB_CRYPTO_FILTERS,
    CB_CRYPTO_HISTORY,
    CB_CRYPTO_MENU,
    CB_CRYPTO_OVERVIEW,
    CB_CRYPTO_SPREAD,
    crypto_arb_keyboard,
    crypto_filters_keyboard,
    crypto_history_keyboard,
    crypto_inline_keyboard,
    crypto_overview_keyboard,
    stub_back_keyboard,
)
from app.telegram.nav import edit_or_reply

logger = logging.getLogger(__name__)

LIVE_CRYPTO_CALLBACKS = frozenset(
    {
        CB_CRYPTO_OVERVIEW,
        CB_CRYPTO_ARB,
        CB_CRYPTO_EXCHANGES,
        CB_CRYPTO_SPREAD,
        CB_CRYPTO_FILTERS,
        CB_CRYPTO_HISTORY,
    }
)

_HUB_LABEL = "Market Intelligence"
_FILTERS_EDIT_KEY = "crypto_filters_edit_field"


def _clear_filters_edit(context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if context is None:
        return
    context.user_data.pop(_FILTERS_EDIT_KEY, None)


def _is_filters_callback(data: str) -> bool:
    return data == CB_CRYPTO_FILTERS or data.startswith("crypto:filters:")


def _confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    confirm_cb = f"crypto:filters:confirm:{token}"
    cancel_cb = f"crypto:filters:cancel:{token}"
    assert len(confirm_cb.encode("utf-8")) <= 64
    assert len(cancel_cb.encode("utf-8")) <= 64
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=confirm_cb),
                InlineKeyboardButton("❌ Cancel", callback_data=cancel_cb),
            ],
            [InlineKeyboardButton(f"⬅️ {_HUB_LABEL}", callback_data=CB_CRYPTO_MENU)],
        ]
    )


def _edit_field_keyboard(field: str, current_values: tuple[str, ...]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for value in current_values:
        cb = f"crypto:filters:set:{field}:{value}"
        if len(cb.encode("utf-8")) > 64:
            continue
        label = value
        if field == "ex":
            label = f"Toggle {value}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    if field in NUMERIC_FIELDS:
        custom_cb = f"crypto:filters:custom:{field}"
        assert len(custom_cb.encode("utf-8")) <= 64
        rows.append(
            [InlineKeyboardButton("Enter custom value", callback_data=custom_cb)]
        )
    rows.append(
        [InlineKeyboardButton("⬅️ Filters", callback_data=CB_CRYPTO_FILTERS)]
    )
    return InlineKeyboardMarkup(rows)


async def handle_crypto_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    settings = context.application.bot_data["settings"]
    back = stub_back_keyboard(hub_callback=CB_CRYPTO_MENU, hub_label=_HUB_LABEL)
    user = update.effective_user
    if user is not None:
        ensure_default_profile(settings, int(user.id))

    try:
        assert_crypto_read_only(settings)
    except RuntimeError:
        await edit_or_reply(
            update,
            f"📊 {_HUB_LABEL}\n"
            "READ-ONLY SAFETY VIOLATION\n"
            "CRYPTO_INTEL_READ_ONLY must remain true.\n"
            "No market-data calls were made.",
            reply_markup=back,
        )
        return

    enabled = crypto_feature_enabled(settings)
    telegram_id = int(user.id) if user is not None else 0

    if _is_filters_callback(data):
        await _handle_filters(update, context, settings, data, telegram_id)
        return

    if data == CB_CRYPTO_HISTORY:
        _clear_filters_edit(context)
        deliveries = storage.list_deliveries_for_user(
            settings, telegram_id=telegram_id, limit=20
        )
        last_sent = storage.list_last_sent_for_user(
            settings, telegram_id=telegram_id, limit=20
        )
        text = format_opportunity_history(deliveries=deliveries, last_sent=last_sent)
        await edit_or_reply(update, text, reply_markup=crypto_history_keyboard())
        return

    if data == CB_CRYPTO_OVERVIEW:
        _clear_filters_edit(context)
        report = collect_market_overview(settings)
        text = format_market_overview(report)
        await edit_or_reply(update, text, reply_markup=crypto_overview_keyboard())
        return

    if data == CB_CRYPTO_ARB:
        _clear_filters_edit(context)
        report = load_top_crypto_opportunities(settings, telegram_id=telegram_id)
        text = format_top_crypto_opportunities(report)
        await edit_or_reply(update, text, reply_markup=crypto_arb_keyboard())
        return

    if data == CB_CRYPTO_EXCHANGES:
        _clear_filters_edit(context)
        rows = probe_all_exchanges(settings)
        text = format_exchanges_health(rows, enabled=enabled)
        await edit_or_reply(update, text, reply_markup=back)
        return

    if data == CB_CRYPTO_SPREAD:
        _clear_filters_edit(context)
        symbol = settings.crypto_symbols[0] if settings.crypto_symbols else "BTC/USDT"
        if not enabled:
            text = format_spread_monitor(symbol, [], enabled=False)
        else:
            results = collect_spread_quotes(settings, symbol=symbol)
            text = format_spread_monitor(symbol, results, enabled=True)
        await edit_or_reply(update, text, reply_markup=back)
        return

    logger.warning("Unhandled crypto callback: %s", data)
    await edit_or_reply(
        update,
        f"📊 {_HUB_LABEL}\nUnknown action.",
        reply_markup=crypto_inline_keyboard(),
    )


async def crypto_filters_text_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Consume the next text message only while a custom Filters edit is pending."""
    field = context.user_data.get(_FILTERS_EDIT_KEY)
    if not field or field not in NUMERIC_FIELDS:
        return False
    message = update.effective_message
    user = update.effective_user
    if message is None or not message.text or user is None:
        return False
    text = message.text.strip()
    if text.startswith("/"):
        return False

    settings = context.application.bot_data["settings"]
    telegram_id = int(user.id)
    ensure_default_profile(settings, telegram_id)
    current = load_settings(settings, telegram_id)
    try:
        proposed = apply_custom_text(current, field=str(field), text=text)
    except Exception as exc:  # noqa: BLE001 — user-facing validation
        await edit_or_reply(
            update,
            f"Rejected: {exc}\n\n{expected_format_hint(str(field))}",
            reply_markup=_edit_field_keyboard(str(field), presets_for_field(str(field))),
        )
        # Keep pending field so the user can retry.
        context.user_data[_FILTERS_EDIT_KEY] = str(field)
        return True

    conf = create_confirmation(
        telegram_id=telegram_id,
        starting=current,
        proposed=proposed,
        changed_keys=[str(field)],
    )
    _clear_filters_edit(context)
    await edit_or_reply(
        update,
        "\n".join(conf.preview_lines),
        reply_markup=_confirm_keyboard(conf.token),
    )
    return True


async def _handle_filters(
    update,
    context: ContextTypes.DEFAULT_TYPE,
    settings,
    data: str,
    telegram_id: int,
) -> None:
    if telegram_id <= 0:
        await edit_or_reply(
            update,
            "Filters require a signed-in Telegram user.",
            reply_markup=crypto_inline_keyboard(),
        )
        return

    if data == CB_CRYPTO_FILTERS:
        _clear_filters_edit(context)
        cfg = load_settings(settings, telegram_id)
        await edit_or_reply(
            update,
            format_filters_summary(cfg),
            reply_markup=crypto_filters_keyboard(),
        )
        return

    if data.startswith("crypto:filters:edit:"):
        field = data.split(":", 3)[-1]
        if field not in FIELD_CODES:
            await edit_or_reply(
                update,
                "Unknown filter field.",
                reply_markup=crypto_filters_keyboard(),
            )
            return
        _clear_filters_edit(context)
        title = FIELD_LABELS.get(field, field)
        presets = presets_for_field(field)
        hint = (
            "Toggle venues in the V4.3 set (Bybit excluded)."
            if field == "ex"
            else "Choose a preset or Enter custom value. Confirm is required before save."
        )
        await edit_or_reply(
            update,
            f"Edit {title}\n{hint}",
            reply_markup=_edit_field_keyboard(field, presets),
        )
        return

    if data.startswith("crypto:filters:custom:"):
        field = data.split(":", 3)[-1]
        if field not in NUMERIC_FIELDS:
            await edit_or_reply(
                update,
                "Custom input is only available for numeric filters.",
                reply_markup=crypto_filters_keyboard(),
            )
            return
        context.user_data[_FILTERS_EDIT_KEY] = field
        await edit_or_reply(
            update,
            field_custom_prompt(field),
            reply_markup=_edit_field_keyboard(field, presets_for_field(field)),
        )
        return

    if data.startswith("crypto:filters:set:"):
        parts = data.split(":")
        # crypto:filters:set:<field>:<value>  -> 5 parts when value has no colon
        if len(parts) < 5:
            await edit_or_reply(
                update,
                "Invalid filter value.",
                reply_markup=crypto_filters_keyboard(),
            )
            return
        field = parts[3]
        value = ":".join(parts[4:])
        _clear_filters_edit(context)
        current = load_settings(settings, telegram_id)
        try:
            proposed = apply_field_set(current, field=field, value=value)
        except Exception as exc:  # noqa: BLE001 — user-facing validation
            await edit_or_reply(
                update,
                f"Rejected: {exc}",
                reply_markup=crypto_filters_keyboard(),
            )
            return
        conf = create_confirmation(
            telegram_id=telegram_id,
            starting=current,
            proposed=proposed,
            changed_keys=[field],
        )
        await edit_or_reply(
            update,
            "\n".join(conf.preview_lines),
            reply_markup=_confirm_keyboard(conf.token),
        )
        return

    if data.startswith("crypto:filters:confirm:"):
        token = data.split(":", 3)[-1]
        _clear_filters_edit(context)
        status, saved = execute_confirmation(
            settings, telegram_id=telegram_id, token=token
        )
        if status != "ok" or saved is None:
            await edit_or_reply(
                update,
                f"Confirm failed ({status}). No changes saved.",
                reply_markup=crypto_filters_keyboard(),
            )
            return
        await edit_or_reply(
            update,
            "Filters saved.\n\n" + format_filters_summary(saved),
            reply_markup=crypto_filters_keyboard(),
        )
        return

    if data.startswith("crypto:filters:cancel:"):
        token = data.split(":", 3)[-1]
        _clear_filters_edit(context)
        cancel_confirmation(telegram_id=telegram_id, token=token)
        cfg = load_settings(settings, telegram_id)
        await edit_or_reply(
            update,
            "Cancelled. No changes saved.\n\n" + format_filters_summary(cfg),
            reply_markup=crypto_filters_keyboard(),
        )
        return

    await edit_or_reply(
        update,
        "Unknown filters action.",
        reply_markup=crypto_filters_keyboard(),
    )
