from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.config.settings import Settings
from app.copy_intel.scheduler import schedule_copy_intel_jobs
from app.database.storage import initialize_database
from app.health.scheduler import schedule_health_jobs
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.copy_intel.handlers import (
    copy_add_command,
    copy_alerts_command,
    copy_command,
    copy_discover_command,
    copy_list_command,
    copy_paper_command,
    copy_remove_command,
    copy_settings_command,
    copy_settings_text_input,
    copy_status_command,
    copy_toggle_command,
)
from app.telegram.opportunity_intel.handlers import (
    opportunities_command,
    opportunity_settings_text_input,
)
from app.telegram.handlers import (
    analyze_command,
    error_handler,
    help_command,
    menu_command,
    recent_command,
    scan_command,
    schedule_report_jobs,
    start_command,
    status_command,
    top_command,
    weather_command,
)
from app.telegram.health.handlers import health_command


def _build_application(token: str) -> Application:
    """Build Application with JobQueue when the optional job-queue extra is installed."""
    builder = Application.builder().token(token)
    try:
        from telegram.ext import JobQueue

        return builder.job_queue(JobQueue()).build()
    except Exception:
        return builder.build()


def build_telegram_application(settings: Settings) -> Application:
    initialize_database(settings)

    app = _build_application(settings.telegram_token)
    app.bot_data["settings"] = settings

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("recent", recent_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("copy", copy_command))
    app.add_handler(CommandHandler("copy_list", copy_list_command))
    app.add_handler(CommandHandler("copy_add", copy_add_command))
    app.add_handler(CommandHandler("copy_remove", copy_remove_command))
    app.add_handler(CommandHandler("copy_toggle", copy_toggle_command))
    app.add_handler(CommandHandler("copy_status", copy_status_command))
    app.add_handler(CommandHandler("copy_alerts", copy_alerts_command))
    app.add_handler(CommandHandler("copy_paper", copy_paper_command))
    app.add_handler(CommandHandler("copy_discover", copy_discover_command))
    app.add_handler(CommandHandler("copy_settings", copy_settings_command))
    app.add_handler(CommandHandler("opportunities", opportunities_command))
    # Single callback router — avoids duplicate/conflicting callback handlers.
    app.add_handler(CallbackQueryHandler(inline_callback_handler))
    # Gated settings numeric/text input (only acts when edit field pending).
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _settings_text_router),
        group=1,
    )
    app.add_error_handler(error_handler)

    schedule_report_jobs(app, settings)
    schedule_copy_intel_jobs(app, settings)
    schedule_health_jobs(app, settings)
    return app


async def _settings_text_router(update, context):
    if await opportunity_settings_text_input(update, context):
        return
    await copy_settings_text_input(update, context)
