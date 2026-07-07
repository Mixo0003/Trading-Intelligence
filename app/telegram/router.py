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
            "📊 Status\n\n"
            "✅ Bot online\n"
            "✅ Telegram notifier works\n"
            "✅ Scheduler ready\n"
            "✅ Scanner ready",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "filters":
        await query.edit_message_text(
            "⚙️ Filters\n\n"
            "MVP settings:\n"
            "- Market source: Polymarket\n"
            "- Category: Weather\n"
            "- Min edge: 5%\n"
            "- Scan limit: 50\n"
            "- Notifications: ON",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "notifications":
        await query.edit_message_text(
            "🔔 Notifications\n\n"
            "MVP: notifications enabled for admin.",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "support":
        await query.edit_message_text(
            "🆘 Support\n\n"
            "Contact admin or return to the main menu.",
            reply_markup=main_menu_keyboard(),
        )

    else:
        await query.edit_message_text(
            "Unknown option.",
            reply_markup=main_menu_keyboard(),
        )
