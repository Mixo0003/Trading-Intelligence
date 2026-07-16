from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.config.settings import Settings
from app.database.storage import initialize_database
from app.telegram.buttons import ALL_NAV_BUTTONS
from app.telegram.handlers import (
    analyze_command,
    button_message_handler,
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


def _build_application(token: str) -> Application:
    """Build Application with JobQueue when the optional job-queue extra is installed."""
    builder = Application.builder().token(token)
    try:
        from telegram.ext import JobQueue

        # Explicit JobQueue so scheduled reports work when apscheduler extra is present.
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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.Text(ALL_NAV_BUTTONS), button_message_handler))
    app.add_error_handler(error_handler)

    schedule_report_jobs(app, settings)
    return app
