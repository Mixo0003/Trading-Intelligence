"""Shared Telegram navigation helpers (edit-in-place + callback answers)."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardMarkup, Message, Update

from app.config.settings import Settings
from app.telegram.auth import is_admin
from app.telegram.inline_menus import (
    MAIN_MENU_TEXT,
    main_inline_keyboard,
    remove_reply_keyboard,
)

logger = logging.getLogger(__name__)


async def answer_callback(update: Update) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except Exception as exc:
        logger.warning("Could not answer callback query: %s", exc)


async def edit_or_reply(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Edit the current callback message when possible; otherwise reply."""
    query = update.callback_query
    if query is not None and query.message is not None:
        try:
            return await query.message.edit_text(text, reply_markup=reply_markup)
        except Exception as exc:
            if "Message is not modified" in str(exc):
                return query.message
            logger.warning("edit_text failed, falling back to reply: %s", exc)
    message = update.effective_message
    if message is not None:
        return await message.reply_text(text, reply_markup=reply_markup)
    return None


def _admin_flag(update: Update, settings: Settings | None) -> bool:
    if settings is None:
        return False
    user = update.effective_user
    return is_admin(settings, user.id if user else None)


async def open_main_menu(
    update: Update,
    *,
    from_command: bool = False,
    settings: Settings | None = None,
) -> None:
    """Show main inline menu; clear any old reply keyboard on command entry."""
    admin = _admin_flag(update, settings)
    keyboard = main_inline_keyboard(is_admin=admin)
    if update.callback_query is not None and not from_command:
        await edit_or_reply(update, MAIN_MENU_TEXT, reply_markup=keyboard)
        return
    message = update.effective_message
    if message is None:
        return
    await message.reply_text("Menu", reply_markup=remove_reply_keyboard())
    await message.reply_text(MAIN_MENU_TEXT, reply_markup=keyboard)
