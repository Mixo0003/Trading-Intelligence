"""Admin-only System Health command and callback handlers."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.health.monitor import run_health_check
from app.telegram.auth import require_admin
from app.telegram.health.buttons import (
    CB_HEALTH,
    CB_HEALTH_BACKUP,
    CB_HEALTH_BACKUP_CANCEL,
    CB_HEALTH_BACKUP_CONFIRM,
    CB_HEALTH_BACKUP_CREATE,
    CB_HEALTH_COPY,
    CB_HEALTH_DB,
    CB_HEALTH_INCIDENTS,
    CB_HEALTH_JOBS,
    CB_HEALTH_REBUILD_BASELINES,
    CB_HEALTH_REBUILD_CANCEL,
    CB_HEALTH_REBUILD_CONFIRM,
    CB_HEALTH_RETENTION,
    CB_HEALTH_SELFTEST,
    CB_HEALTH_SETTINGS,
    CB_HEALTH_SUMMARY,
    CB_HEALTH_WEATHER,
    HEALTH_MENU_TEXT,
    backup_confirm_keyboard,
    backup_health_keyboard,
    copy_health_keyboard,
    health_back_keyboard,
    health_inline_keyboard,
    rebuild_baselines_confirm_keyboard,
)
from app.telegram.health.formatting import (
    format_backup_confirm,
    format_backup_result,
    format_backup_status,
    format_copy_health,
    format_db_health,
    format_health_settings,
    format_health_summary,
    format_incidents,
    format_jobs_health,
    format_rebuild_baselines_confirm,
    format_rebuild_baselines_result,
    format_retention_preview,
    format_self_test,
    format_weather_health,
)
from app.telegram.nav import edit_or_reply

_BACKUP_IN_FLIGHT_KEY = "health_backup_in_flight"


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

    if data == CB_HEALTH_REBUILD_BASELINES:
        await edit_or_reply(
            update,
            format_rebuild_baselines_confirm(settings),
            reply_markup=rebuild_baselines_confirm_keyboard(),
        )
        return

    if data == CB_HEALTH_REBUILD_CANCEL:
        overall = run_health_check(settings, bot_data=context.application.bot_data)
        await edit_or_reply(
            update,
            format_copy_health(settings, overall),
            reply_markup=copy_health_keyboard(),
        )
        return

    if data == CB_HEALTH_REBUILD_CONFIRM:
        from app.copy_intel.freshness import rebuild_all_copy_baselines

        result = rebuild_all_copy_baselines(settings)
        await edit_or_reply(
            update,
            format_rebuild_baselines_result(result),
            reply_markup=copy_health_keyboard(),
        )
        return

    if data == CB_HEALTH_BACKUP:
        await edit_or_reply(
            update,
            format_backup_status(settings),
            reply_markup=backup_health_keyboard(),
        )
        return

    if data == CB_HEALTH_BACKUP_CREATE:
        if not await require_admin(update, settings, action="cb:health_backup_create"):
            return
        await edit_or_reply(
            update,
            format_backup_confirm(settings),
            reply_markup=backup_confirm_keyboard(),
        )
        return

    if data == CB_HEALTH_BACKUP_CANCEL:
        if not await require_admin(update, settings, action="cb:health_backup_cancel"):
            return
        await edit_or_reply(
            update,
            format_backup_status(settings),
            reply_markup=backup_health_keyboard(),
        )
        return

    if data == CB_HEALTH_BACKUP_CONFIRM:
        if not await require_admin(update, settings, action="cb:health_backup_confirm"):
            return
        # Prevent concurrent backups from repeated taps (in addition to DB lock).
        if context.application.bot_data.get(_BACKUP_IN_FLIGHT_KEY):
            await edit_or_reply(
                update,
                "💾 Backup Result\nStatus: BUSY\nDetail: backup already in progress",
                reply_markup=backup_health_keyboard(),
            )
            return
        context.application.bot_data[_BACKUP_IN_FLIGHT_KEY] = True
        try:
            from app.storage.backup import create_online_backup

            report = create_online_backup(settings)
            await edit_or_reply(
                update,
                format_backup_result(report),
                reply_markup=backup_health_keyboard(),
            )
        finally:
            context.application.bot_data[_BACKUP_IN_FLIGHT_KEY] = False
        return

    overall = run_health_check(settings, bot_data=context.application.bot_data)
    back = health_back_keyboard()

    if data == CB_HEALTH_SUMMARY:
        text = format_health_summary(overall)
    elif data == CB_HEALTH_WEATHER:
        text = format_weather_health(settings)
    elif data == CB_HEALTH_COPY:
        text = format_copy_health(settings, overall)
        await edit_or_reply(update, text, reply_markup=copy_health_keyboard())
        return
    elif data == CB_HEALTH_JOBS:
        text = format_jobs_health(settings)
    elif data == CB_HEALTH_DB:
        text = format_db_health(settings, overall)
    elif data == CB_HEALTH_RETENTION:
        # Dedicated admin dry-run; do not rely on overall probe side effects.
        text = format_retention_preview(settings)
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
