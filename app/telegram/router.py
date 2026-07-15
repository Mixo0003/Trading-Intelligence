"""Deprecated legacy callback router — not registered by build_telegram_application."""

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import load_settings
from app.signals.scanner import scan_polymarket_weather_markets
from app.telegram.buttons import main_menu_keyboard


def _format_scan_result(total_markets: int, signals_count: int) -> str:
    return (
        "📡 Scan finished\n\n"
        f"Markets checked: {total_markets}\n"
        f"Signals found: {signals_count}"
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy inline-callback handler. Use ReplyKeyboard navigation in handlers.py."""
    query = update.callback_query

    if query is None:
        return

    await query.answer()

    data = query.data

    if data == "scan_now":
        await query.edit_message_text(
            "📡 Scan Now\n\nScanning Polymarket Weather markets...",
            reply_markup=main_menu_keyboard(),
        )

        try:
            settings = load_settings()
            result = scan_polymarket_weather_markets(
                settings=settings,
                limit=50,
                min_edge=0.05,
            )

            await query.edit_message_text(
                _format_scan_result(
                    total_markets=result.total_markets,
                    signals_count=len(result.signals),
                ),
                reply_markup=main_menu_keyboard(),
            )

        except Exception as exc:
            await query.edit_message_text(
                "❌ Scan failed\n\n"
                f"Error: {exc}",
                reply_markup=main_menu_keyboard(),
            )

    elif data == "status":
        await query.edit_message_text(
            "📊 System Status\n\n"
            "Bot: Online\n"
            "Telegram UI: Active\n"
            "Manual scanner: Available via Scan Now\n"
            "Scheduler: Separate process, not verified from this screen\n"
            "Last scan: shown after manual Scan Now\n\n"
            "Note: this screen only shows what the Telegram process can verify.",
            reply_markup=main_menu_keyboard(),
        )

    elif data in {"filters", "notifications", "support"}:
        await query.edit_message_text(
            "This menu is obsolete. Use the bottom keyboard or /menu.",
            reply_markup=main_menu_keyboard(),
        )

    else:
        await query.edit_message_text(
            "Unknown option. Use /menu for the current keyboard.",
            reply_markup=main_menu_keyboard(),
        )
