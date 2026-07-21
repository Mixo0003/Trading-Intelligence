"""Admin-only System Health command and callback handlers."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.health.monitor import run_health_check
from app.storage.retention_policies import build_retention_policies
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
    CB_HEALTH_STORAGE,
    CB_HEALTH_STORAGE_BACKUP,
    CB_HEALTH_STORAGE_BACKUP_CANCEL,
    CB_HEALTH_STORAGE_BACKUP_CONFIRM,
    CB_HEALTH_STORAGE_BACKUP_CREATE,
    CB_HEALTH_STORAGE_CLEANUP_CANCEL,
    CB_HEALTH_STORAGE_CLEANUP_CONFIRM,
    CB_HEALTH_STORAGE_CLEANUP_PREVIEW,
    CB_HEALTH_STORAGE_CLEANUP_RUN,
    CB_HEALTH_STORAGE_HISTORY,
    CB_HEALTH_STORAGE_HISTORY_PAGE,
    CB_HEALTH_STORAGE_METRICS,
    CB_HEALTH_STORAGE_PRUNE_CANCEL,
    CB_HEALTH_STORAGE_PRUNE_CONFIRM,
    CB_HEALTH_STORAGE_PRUNE_RUN,
    CB_HEALTH_STORAGE_REFRESH,
    CB_HEALTH_STORAGE_RETENTION_PAGE,
    CB_HEALTH_STORAGE_RETENTION_PREVIEW,
    CB_HEALTH_SUMMARY,
    CB_HEALTH_WEATHER,
    HEALTH_MENU_TEXT,
    backup_confirm_keyboard,
    backup_health_keyboard,
    copy_health_keyboard,
    health_back_keyboard,
    health_inline_keyboard,
    rebuild_baselines_confirm_keyboard,
    storage_back_keyboard,
    storage_backup_confirm_keyboard,
    storage_cleanup_confirm_keyboard,
    storage_inline_keyboard,
    storage_prune_confirm_keyboard,
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
    format_maintenance_history,
    format_rebuild_baselines_confirm,
    format_rebuild_baselines_result,
    format_retention_preview,
    format_self_test,
    format_storage_backup_confirm,
    format_storage_cleanup_confirm,
    format_storage_cleanup_preview,
    format_storage_cleanup_result,
    format_storage_menu,
    format_storage_metrics,
    format_storage_prune_confirm,
    format_storage_prune_result,
    format_weather_health,
)
from app.telegram.nav import edit_or_reply

logger = logging.getLogger(__name__)

_BACKUP_IN_FLIGHT_KEY = "health_backup_in_flight"
_STORAGE_BACKUP_TOKEN_KEY = "storage_backup_confirm_token"
_STORAGE_CLEANUP_TOKEN_KEY = "storage_cleanup_confirm_token"
_STORAGE_PRUNE_TOKEN_KEY = "storage_prune_confirm_token"
_STORAGE_BACKUP_IN_FLIGHT = "storage_backup_in_flight"
_STORAGE_CLEANUP_IN_FLIGHT = "storage_cleanup_in_flight"
_STORAGE_PRUNE_IN_FLIGHT = "storage_prune_in_flight"


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _approved_datasets() -> list[str]:
    return [p.dataset for p in build_retention_policies() if p.kind == "APPROVED"]


def _operator_id(update: Update) -> str:
    user = update.effective_user
    return str(user.id if user is not None else "")


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


async def _show_storage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    overall = run_health_check(settings, bot_data=context.application.bot_data)
    await edit_or_reply(
        update,
        format_storage_menu(settings, overall),
        reply_markup=storage_inline_keyboard(),
    )


async def _run_background(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    *,
    in_flight_key: str,
    progress_text: str,
    work,
    format_result,
    reply_markup,
) -> None:
    """Answer already handled by router; edit progress then final report once."""
    if context.application.bot_data.get(in_flight_key):
        await edit_or_reply(
            update,
            progress_text.replace("in progress…", "BUSY — already in progress"),
            reply_markup=reply_markup,
        )
        return
    context.application.bot_data[in_flight_key] = True
    await edit_or_reply(update, progress_text, reply_markup=reply_markup)
    chat_id = None
    message_id = None
    query = update.callback_query
    if query is not None and query.message is not None:
        chat_id = query.message.chat_id
        message_id = query.message.message_id

    async def _task() -> None:
        try:
            report = await asyncio.get_running_loop().run_in_executor(None, work)
            text = format_result(report)
            if chat_id is not None and message_id is not None:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=reply_markup,
                    )
                    return
                except Exception:
                    logger.debug("edit final storage report failed", exc_info=True)
            if chat_id is not None:
                await context.bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup
                )
        except Exception as exc:
            logger.exception("Storage background task failed")
            fail = (
                f"Operation failed\nReason: {type(exc).__name__}\n"
                "No private paths or secrets shown."
            )
            if chat_id is not None and message_id is not None:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=fail,
                        reply_markup=reply_markup,
                    )
                    return
                except Exception:
                    pass
            if chat_id is not None:
                await context.bot.send_message(
                    chat_id=chat_id, text=fail, reply_markup=reply_markup
                )
        finally:
            context.application.bot_data[in_flight_key] = False

    asyncio.create_task(_task())


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

    if data in {CB_HEALTH_STORAGE, CB_HEALTH_STORAGE_REFRESH}:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        await _show_storage_menu(update, context)
        return

    if data == CB_HEALTH_STORAGE_METRICS:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        overall = run_health_check(settings, bot_data=context.application.bot_data)
        await edit_or_reply(
            update,
            format_storage_metrics(settings, overall),
            reply_markup=storage_back_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_BACKUP:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        await edit_or_reply(
            update,
            format_backup_status(settings),
            reply_markup=storage_back_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_BACKUP_CREATE:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        from app.storage.backup import create_backup_confirmation

        confirmation, _preview = create_backup_confirmation(
            settings, operator_id=_operator_id(update)
        )
        context.user_data[_STORAGE_BACKUP_TOKEN_KEY] = (
            confirmation.token if confirmation else None
        )
        await edit_or_reply(
            update,
            format_storage_backup_confirm(settings),
            reply_markup=storage_backup_confirm_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_BACKUP_CANCEL:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        context.user_data.pop(_STORAGE_BACKUP_TOKEN_KEY, None)
        await _show_storage_menu(update, context)
        return

    if data == CB_HEALTH_STORAGE_BACKUP_CONFIRM:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        from app.storage.backup import execute_confirmed_backup

        token = context.user_data.pop(_STORAGE_BACKUP_TOKEN_KEY, None)
        if not token:
            await edit_or_reply(
                update,
                "💾 Backup Result\nStatus: REFUSED\nDetail: missing confirmation",
                reply_markup=storage_back_keyboard(),
            )
            return

        def _work():
            return execute_confirmed_backup(
                settings, token=token, operator_id=_operator_id(update)
            )

        await _run_background(
            context,
            update,
            in_flight_key=_STORAGE_BACKUP_IN_FLIGHT,
            progress_text='Create Backup\nStatus: in progress...',
            work=_work,
            format_result=format_backup_result,
            reply_markup=storage_back_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_CLEANUP_PREVIEW:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        await edit_or_reply(
            update,
            format_storage_cleanup_preview(settings),
            reply_markup=storage_back_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_RETENTION_PREVIEW or data == CB_HEALTH_STORAGE_RETENTION_PAGE:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        page = int(context.user_data.get("storage_retention_page", 0) or 0)
        if data == CB_HEALTH_STORAGE_RETENTION_PAGE:
            page += 1
            context.user_data["storage_retention_page"] = page
        else:
            context.user_data["storage_retention_page"] = 0
            page = 0
        await edit_or_reply(
            update,
            format_retention_preview(settings, page=page),
            reply_markup=storage_back_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_CLEANUP_RUN:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        from app.storage.cleanup import create_cleanup_confirmation

        confirmation, report = create_cleanup_confirmation(
            settings, operator_id=_operator_id(update)
        )
        context.user_data[_STORAGE_CLEANUP_TOKEN_KEY] = (
            confirmation.token if confirmation else None
        )
        await edit_or_reply(
            update,
            format_storage_cleanup_confirm(settings, report),
            reply_markup=storage_cleanup_confirm_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_CLEANUP_CANCEL:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        context.user_data.pop(_STORAGE_CLEANUP_TOKEN_KEY, None)
        await _show_storage_menu(update, context)
        return

    if data == CB_HEALTH_STORAGE_CLEANUP_CONFIRM:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        from app.storage.cleanup import execute_confirmed_cleanup

        token = context.user_data.pop(_STORAGE_CLEANUP_TOKEN_KEY, None)
        if not token:
            await edit_or_reply(
                update,
                "🗑 File Cleanup Result\nStatus: refused\nDetail: missing confirmation",
                reply_markup=storage_back_keyboard(),
            )
            return

        def _work():
            return execute_confirmed_cleanup(
                settings, token=token, operator_id=_operator_id(update)
            )

        await _run_background(
            context,
            update,
            in_flight_key=_STORAGE_CLEANUP_IN_FLIGHT,
            progress_text="🗑 Safe File Cleanup\nStatus: in progress…",
            work=_work,
            format_result=format_storage_cleanup_result,
            reply_markup=storage_back_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_PRUNE_RUN:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        from app.storage.backup import revalidate_verified_backup_for_pruning
        from app.storage.retention_prune import create_prune_confirmation

        confirmation, report = create_prune_confirmation(
            settings,
            operator_id=_operator_id(update),
            approved_datasets=_approved_datasets(),
        )
        gate = revalidate_verified_backup_for_pruning(
            settings,
            max_age_seconds=settings.storage_retention_prune_max_backup_age_seconds,
        )
        context.user_data[_STORAGE_PRUNE_TOKEN_KEY] = (
            confirmation.token if confirmation else None
        )
        await edit_or_reply(
            update,
            format_storage_prune_confirm(settings, report, gate),
            reply_markup=storage_prune_confirm_keyboard(),
        )
        return

    if data == CB_HEALTH_STORAGE_PRUNE_CANCEL:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        context.user_data.pop(_STORAGE_PRUNE_TOKEN_KEY, None)
        await _show_storage_menu(update, context)
        return

    if data == CB_HEALTH_STORAGE_PRUNE_CONFIRM:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        from app.storage.retention_prune import execute_confirmed_prune

        token = context.user_data.pop(_STORAGE_PRUNE_TOKEN_KEY, None)
        if not token:
            await edit_or_reply(
                update,
                "✂ Retention Batch Result\nStatus: REFUSED\nDetail: missing confirmation",
                reply_markup=storage_back_keyboard(),
            )
            return

        def _work():
            return execute_confirmed_prune(
                settings, token=token, operator_id=_operator_id(update)
            )

        await _run_background(
            context,
            update,
            in_flight_key=_STORAGE_PRUNE_IN_FLIGHT,
            progress_text="✂ Retention Batch\nStatus: in progress…",
            work=_work,
            format_result=format_storage_prune_result,
            reply_markup=storage_back_keyboard(),
        )
        return

    if data in {CB_HEALTH_STORAGE_HISTORY, CB_HEALTH_STORAGE_HISTORY_PAGE}:
        if not await require_admin(update, settings, action=f"cb:{data}"):
            return
        page = int(context.user_data.get("storage_history_page", 0) or 0)
        if data == CB_HEALTH_STORAGE_HISTORY_PAGE:
            page += 1
            context.user_data["storage_history_page"] = page
        else:
            page = 0
            context.user_data["storage_history_page"] = 0
        await edit_or_reply(
            update,
            format_maintenance_history(settings, page=page),
            reply_markup=storage_back_keyboard(),
        )
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
