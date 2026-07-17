"""Admin-only System Health command and callback handlers."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.health.monitor import run_health_check
from app.telegram.auth import require_admin
from app.telegram.health.buttons import (
    CB_HEALTH,
    CB_HEALTH_COPY,
    CB_HEALTH_DB,
    CB_HEALTH_INCIDENTS,
    CB_HEALTH_JOBS,
    CB_HEALTH_SELFTEST,
    CB_HEALTH_SETTINGS,
    CB_HEALTH_SUMMARY,
    CB_HEALTH_WEATHER,
    HEALTH_MENU_TEXT,
    health_back_keyboard,
    health_inline_keyboard,
)
from app.telegram.health.formatting import (
    format_copy_health,
    format_db_health,
    format_health_settings,
    format_health_summary,
    format_incidents,
    format_jobs_health,
    format_self_test,
    format_weather_health,
)
from app.telegram.nav import edit_or_reply


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if not await require_admin(update, settings, action="cmd:/health"):
        return
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(HEALTH_MENU_TEXT, reply_markup=health_inline_keyboard())


async def show_health_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if not await require_admin(update, settings, action="cb:health_menu"):
        return
    await edit_or_reply(update, HEALTH_MENU_TEXT, reply_markup=health_inline_keyboard())


async def handle_health_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    settings = _settings(context)
    if not await require_admin(update, settings, action=f"cb:{data}"):
        return

    if data == CB_HEALTH:
        await edit_or_reply(update, HEALTH_MENU_TEXT, reply_markup=health_inline_keyboard())
        return

    overall = run_health_check(settings, bot_data=context.application.bot_data)
    back = health_back_keyboard()

    if data == CB_HEALTH_SUMMARY:
        text = format_health_summary(overall)
    elif data == CB_HEALTH_WEATHER:
        text = format_weather_health(settings)
    elif data == CB_HEALTH_COPY:
        text = format_copy_health(settings, overall)
    elif data == CB_HEALTH_JOBS:
        text = format_jobs_health(settings)
    elif data == CB_HEALTH_DB:
        text = format_db_health(settings, overall)
    elif data == CB_HEALTH_INCIDENTS:
        text = format_incidents(settings)
    elif data == CB_HEALTH_SELFTEST:
        if not await require_admin(update, settings, action="cb:health_selftest"):
            return
        text = format_self_test(settings, bot_data=context.application.bot_data)
    elif data == CB_HEALTH_SETTINGS:
        text = format_health_settings(settings)
    else:
        text = HEALTH_MENU_TEXT
        await edit_or_reply(update, text, reply_markup=health_inline_keyboard())
        return

    await edit_or_reply(update, text, reply_markup=back)
