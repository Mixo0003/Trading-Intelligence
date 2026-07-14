import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import load_settings
from app.database.storage import (
    load_latest_completed_weather_scan,
    save_completed_weather_scan,
)
from app.telegram.buttons import main_menu_keyboard
from app.telegram.weather_views import (
    format_scan,
    format_today_forecasts,
    format_top_signals,
)
from app.weather.market_forecasts import scan_temperature_forecasts


def _telegram_chunks(text: str, limit: int = 4000) -> list[str]:
    chunks: list[str] = []
    current = ""
    for section in text.split("\n\n"):
        candidate = f"{current}\n\n{section}" if current else section
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = section
    if current:
        chunks.append(current)
    return chunks or [text]


async def _send_final(query, text: str) -> None:
    chunks = _telegram_chunks(text)
    try:
        await query.edit_message_text(chunks[0], reply_markup=main_menu_keyboard())
        remaining = chunks[1:]
    except Exception:
        # A deleted/expired callback message must not prevent delivery of the
        # final result.
        remaining = chunks
    for chunk in remaining:
        await query.message.reply_text(chunk, reply_markup=main_menu_keyboard())


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
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    scan_temperature_forecasts,
                    settings,
                    200,
                    12,
                ),
                timeout=75.0,
            )
            await asyncio.wait_for(
                asyncio.to_thread(save_completed_weather_scan, settings, result),
                timeout=5.0,
            )

            await _send_final(query, format_scan(result))

        except asyncio.TimeoutError:
            await _send_final(
                query,
                "❌ Scan failed\n\nThe bounded scan timed out; no incomplete snapshot was saved.",
            )
        except Exception as exc:
            await _send_final(
                query,
                "❌ Scan failed\n\n"
                f"Error: {type(exc).__name__}",
            )

    elif data == "top_signals":
        await query.edit_message_text(
            "Loading top signals...",
            reply_markup=main_menu_keyboard(),
        )
        try:
            settings = load_settings()
            result = await asyncio.wait_for(
                asyncio.to_thread(load_latest_completed_weather_scan, settings),
                timeout=0.8,
            )
            text = (
                format_top_signals(result)
                if result is not None
                else "📈 TOP SIGNALS\n\nNo completed scan is stored.\n\nUse 🔍 Scan Now to refresh."
            )
        except Exception as exc:
            text = f"📈 TOP SIGNALS\n\nStored scan could not be read ({type(exc).__name__})."
        await _send_final(query, text)

    elif data == "today_forecast":
        await query.edit_message_text(
            "Loading today’s market forecast...",
            reply_markup=main_menu_keyboard(),
        )
        try:
            settings = load_settings()
            result = await asyncio.wait_for(
                asyncio.to_thread(load_latest_completed_weather_scan, settings),
                timeout=2.0,
            )
            if result is None:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        scan_temperature_forecasts,
                        settings,
                        25,
                        8,
                    ),
                    timeout=45.0,
                )
                await asyncio.wait_for(
                    asyncio.to_thread(save_completed_weather_scan, settings, result),
                    timeout=5.0,
                )
            text = format_today_forecasts(result)
        except asyncio.TimeoutError:
            text = (
                "📅 TODAY’S MARKET FORECASTS\n\n"
                "The bounded forecast refresh timed out. Please use 🔍 Scan Now to refresh."
            )
        except Exception as exc:
            text = (
                "📅 TODAY’S MARKET FORECASTS\n\n"
                f"Stored forecasts could not be read ({type(exc).__name__})."
            )
        await _send_final(query, text)

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
