import asyncio
import logging
from datetime import datetime, time, timezone

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.config.timezones import load_timezone
from app.database.storage import get_recent_analyses, upsert_user
from app.polymarket.client import PolymarketClient
from app.polymarket.discovery_errors import WeatherDiscoveryError
from app.region.session import get_user_scan_region
from app.region.types import ScanRegion
from app.signals.scan_service import (
    analyze_market_by_url,
    scan_weather_markets,  # noqa: F401 — re-exported for test patches
    scan_weather_markets_async,
)
from app.telegram.inline_menus import (
    WEATHER_MENU_TEXT,
    main_inline_keyboard,
    remove_reply_keyboard,
    weather_inline_keyboard,
)
from app.telegram.auth import is_admin
from app.telegram.nav import edit_or_reply, open_main_menu
from app.telegram.formatting import (
    format_concise_opportunity,
    format_opportunities_bundle,
    format_scan_delivery_messages,
    format_scan_report,
    format_scheduled_report,
    format_today_forecast_from_snapshot,
    format_top_opportunities,
    format_top_signals_report,
    format_top_signals_unavailable,
    persist_scan_results,
)
from app.telegram.scan_cache import load_latest_scan_snapshot, save_scan_snapshot
from app.telegram.scan_tasks import try_start_scoped_task
from app.telegram.messages import HELP_TEXT, START_TEXT

logger = logging.getLogger(__name__)

TOP_SIGNALS_TIMEOUT_SECONDS = 5
FORECAST_VIEW_TIMEOUT_SECONDS = 5
SCHEDULED_REPORTS_REGISTERED_KEY = "scheduled_reports_registered"
SCAN_STARTED_MESSAGE = "🔍 Scan started…"
SCAN_ALREADY_RUNNING_MESSAGE = "A scan for this region is already running."
SCAN_DISCOVERY_UNAVAILABLE_MESSAGE = (
    "❌ Market discovery is temporarily unavailable. Please retry shortly."
)
SCAN_FAILED_MESSAGE = "❌ Scan failed. Please retry shortly."


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _bot_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data


def _main_keyboard(update: Update, settings: Settings):
    user = update.effective_user
    return main_inline_keyboard(
        is_admin=is_admin(settings, user.id if user else None)
    )


async def _run_scan(settings: Settings, region: ScanRegion):
    """Shared safe scan execution path for interactive and scheduled scans."""
    return await scan_weather_markets_async(settings, region=region)


async def _track_user(update: Update, settings: Settings) -> None:
    user = update.effective_user
    if user is None:
        return
    upsert_user(
        settings=settings,
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )


def _reply_markup_for_region(region: ScanRegion):
    return weather_inline_keyboard()


async def _replace_loading_message(
    loading_message,
    source_message,
    final_text: str,
    *,
    reply_markup,
) -> None:
    """Replace a progress message, deleting and replying only if editing fails."""
    try:
        await loading_message.edit_text(final_text, reply_markup=reply_markup)
        return
    except Exception as exc:
        logger.warning("Could not edit Telegram loading message: %s", exc)

    try:
        await loading_message.delete()
    except Exception as exc:
        logger.warning("Could not delete Telegram loading message: %s", exc)

    await source_message.reply_text(final_text, reply_markup=reply_markup)


async def _load_top_signals_response(
    settings: Settings,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    telegram_id: int | None,
    region: ScanRegion,
) -> str:
    if telegram_id is None:
        return format_top_signals_unavailable(region)
    snapshot = load_latest_scan_snapshot(
        settings,
        telegram_id,
        bot_data=_bot_data(context),
        region=region,
    )
    if snapshot is None:
        return format_top_signals_unavailable(region)
    return format_top_signals_report(snapshot)


async def _reply_top_signals(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    telegram_id: int | None,
    region: ScanRegion,
    reply_markup,
    loading_text: str | None = None,
) -> None:
    settings = _settings(context)
    loading = loading_text or f"📈 Loading {region.display_label} top signals..."
    loading_message = await message.reply_text(loading, reply_markup=reply_markup)
    try:
        response = await asyncio.wait_for(
            _load_top_signals_response(
                settings,
                context,
                telegram_id=telegram_id,
                region=region,
            ),
            timeout=TOP_SIGNALS_TIMEOUT_SECONDS,
        )
        await _replace_loading_message(
            loading_message,
            message,
            response,
            reply_markup=reply_markup,
        )
    except asyncio.TimeoutError:
        logger.warning("Top signals read timed out after %ss", TOP_SIGNALS_TIMEOUT_SECONDS)
        await _replace_loading_message(
            loading_message,
            message,
            f"📈 {region.display_label.upper()} TOP SIGNALS\n\n"
            f"Could not load cached results in time. Use {region.scan_button} to refresh.",
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logger.exception("Top signals failed: %s", exc)
        await _replace_loading_message(
            loading_message,
            message,
            f"📈 {region.display_label.upper()} TOP SIGNALS\n\n"
            f"Could not load cached results. Use {region.scan_button} to refresh.",
            reply_markup=reply_markup,
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    await _track_user(update, settings)
    message = update.message
    if message is None:
        return
    from app.telegram.inline_menus import MAIN_MENU_TEXT

    await message.reply_text(START_TEXT, reply_markup=remove_reply_keyboard())
    await message.reply_text(
        MAIN_MENU_TEXT,
        reply_markup=_main_keyboard(update, settings),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await open_main_menu(update, from_command=True, settings=_settings(context))


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    if message is None:
        return
    await message.reply_text(WEATHER_MENU_TEXT, reply_markup=weather_inline_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        settings = _settings(context)
        await update.message.reply_text(
            HELP_TEXT,
            reply_markup=_main_keyboard(update, settings),
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    polymarket_status = "unknown"
    try:
        client = PolymarketClient(settings)
        client.get_json("/markets", params={"limit": 1})
        polymarket_status = "ok"
    except Exception:
        polymarket_status = "error"

    region = get_user_scan_region(context)
    scheduler_status = "configured" if settings.scheduled_reports_configured else "disabled"
    report_regions = ", ".join(r.value for r in settings.auto_report_regions) or "none"
    text = (
        "📊 Status\n\n"
        "Bot: online\n"
        f"Polymarket API: {polymarket_status}\n"
        "Weather providers: NWS, Weather.com, MET Norway\n"
        "EV engine: ok\n"
        f"Selected region: {region.display_label}\n"
        f"Scheduler: {scheduler_status}\n"
        f"Auto-report regions: {report_regions}\n"
        "Mode: ANALYSIS ONLY"
    )
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=_main_keyboard(update, settings),
        )


async def _start_region_scan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    region: ScanRegion,
) -> None:
    settings = _settings(context)
    await _track_user(update, settings)
    message = update.effective_message
    if message is None:
        return
    reply_markup = weather_inline_keyboard()
    scope = region.value

    user = update.effective_user
    chat_id = message.chat_id if hasattr(message, "chat_id") else message.chat.id
    bot = context.bot
    bot_data = _bot_data(context)

    async def _background_scan():
        try:
            progress = await _run_scan(settings, region)
            if user:
                persist_scan_results(settings, user.id, progress)
                save_scan_snapshot(
                    settings,
                    user.id,
                    progress,
                    bot_data=bot_data,
                    region=region,
                )
            chunks = format_scan_delivery_messages(progress)
            for i, chunk in enumerate(chunks):
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    reply_markup=reply_markup if i == len(chunks) - 1 else None,
                )
        except WeatherDiscoveryError:
            logger.warning("Background scan discovery unavailable for %s", scope)
            await bot.send_message(
                chat_id=chat_id,
                text=SCAN_DISCOVERY_UNAVAILABLE_MESSAGE,
                reply_markup=reply_markup,
            )
        except Exception as exc:
            logger.exception("Background scan failed for %s: %s", scope, type(exc).__name__)
            await bot.send_message(
                chat_id=chat_id,
                text=SCAN_FAILED_MESSAGE,
                reply_markup=reply_markup,
            )

    started, _task = await try_start_scoped_task(scope, _background_scan)
    if not started:
        if update.callback_query is not None:
            await edit_or_reply(update, SCAN_ALREADY_RUNNING_MESSAGE, reply_markup=reply_markup)
        else:
            await message.reply_text(SCAN_ALREADY_RUNNING_MESSAGE, reply_markup=reply_markup)
        return

    if update.callback_query is not None:
        await edit_or_reply(
            update,
            f"{SCAN_STARTED_MESSAGE}\nRegion: {region.display_label}",
            reply_markup=reply_markup,
        )
    else:
        await message.reply_text(
            f"{SCAN_STARTED_MESSAGE}\nRegion: {region.display_label}",
            reply_markup=reply_markup,
        )


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    region = get_user_scan_region(context)
    await _start_region_scan(update, context, region)


async def scan_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    region = get_user_scan_region(context)
    await _start_region_scan(update, context, region)


async def _load_top_text(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, region: ScanRegion) -> str:
    settings = _settings(context)
    snapshot = load_latest_scan_snapshot(
        settings,
        telegram_id,
        bot_data=_bot_data(context),
        region=region,
    )
    if snapshot is None:
        return format_top_signals_unavailable(region)
    return format_top_opportunities(snapshot.progress)


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    if message is None:
        return
    region = get_user_scan_region(context)
    telegram_id = update.effective_user.id if update.effective_user else message.chat.id
    reply_markup = _main_keyboard(update, _settings(context))
    loading_message = await message.reply_text(
        f"📊 Loading {region.display_label} opportunities...",
    )
    try:
        text = await _load_top_text(context, telegram_id, region)
        await _replace_loading_message(
            loading_message,
            message,
            text,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logger.exception("Top command failed: %s", exc)
        await _replace_loading_message(
            loading_message,
            message,
            "❌ Could not load opportunities. Run a regional scan first.",
            reply_markup=reply_markup,
        )


async def top_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    region = get_user_scan_region(context)
    telegram_id = update.effective_user.id if update.effective_user else 0
    try:
        text = await _load_top_text(context, telegram_id, region)
    except Exception:
        text = "❌ Could not load opportunities."
    await edit_or_reply(
        update,
        text,
        reply_markup=_main_keyboard(update, _settings(context)),
    )


async def top_signals_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    region = get_user_scan_region(context)
    telegram_id = update.effective_user.id if update.effective_user else 0
    message = update.effective_message
    if message is None:
        return
    text = await _load_top_signals_response(
        _settings(context),
        context,
        telegram_id=telegram_id,
        region=region,
    )
    await edit_or_reply(update, text, reply_markup=weather_inline_keyboard())


async def forecast_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    region = get_user_scan_region(context)
    telegram_id = update.effective_user.id if update.effective_user else 0

    def _load_forecast_text() -> str:
        snapshot = load_latest_scan_snapshot(
            settings,
            telegram_id,
            bot_data=_bot_data(context),
            region=region,
        )
        if snapshot is None:
            return format_top_signals_unavailable(region).replace(
                "No completed",
                f"📅 {region.display_label.upper()} FORECASTS\n\nNo completed",
            )
        return format_today_forecast_from_snapshot(snapshot)

    try:
        text = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _load_forecast_text),
            timeout=FORECAST_VIEW_TIMEOUT_SECONDS,
        )
    except Exception:
        text = f"📅 {region.display_label.upper()} FORECASTS\n\nCould not load cached forecasts."
    await edit_or_reply(update, text, reply_markup=weather_inline_keyboard())


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    message = update.message
    if message is None:
        return
    if not context.args:
        await message.reply_text(
            "Usage: /analyze https://polymarket.com/event/...\n\nOptional manual fallback only.",
            reply_markup=_main_keyboard(update, settings),
        )
        return
    url = context.args[0]
    reply_markup = _main_keyboard(update, settings)
    loading_message = await message.reply_text(
        "Analyzing URL (manual fallback)...",
        reply_markup=reply_markup,
    )
    try:
        loop = asyncio.get_running_loop()
        analysis = await loop.run_in_executor(None, analyze_market_by_url, settings, url)
        await _replace_loading_message(
            loading_message,
            message,
            format_concise_opportunity(analysis),
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logger.exception("Analyze command failed: %s", exc)
        await _replace_loading_message(
            loading_message,
            message,
            "❌ Could not analyze URL.",
            reply_markup=reply_markup,
        )


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    rows = get_recent_analyses(settings, limit=10)
    if not rows:
        text = "🕘 No recent analyses recorded yet. Run a scan first."
    else:
        lines = ["🕘 Recent Reports", ""]
        for row in rows:
            ev = row["ev"]
            ev_text = f"{ev * 100:+.0f}%" if ev is not None else "N/A"
            lines.append(f"• {row['decision']} | EV {ev_text} | {row['market_title']}")
        text = "\n".join(lines)
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=_main_keyboard(update, settings),
        )


async def forecast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    region = get_user_scan_region(context)
    message = update.message or update.effective_message
    if message is None:
        return
    user = update.effective_user
    telegram_id = user.id if user else message.chat.id
    reply_markup = _reply_markup_for_region(region)
    loading_message = await message.reply_text(
        f"📅 Loading {region.display_label} forecasts...",
        reply_markup=reply_markup,
    )
    try:
        def _load_forecast_text() -> str:
            snapshot = load_latest_scan_snapshot(
                settings,
                telegram_id,
                bot_data=_bot_data(context),
                region=region,
            )
            if snapshot is None:
                return format_top_signals_unavailable(region).replace(
                    "No completed",
                    f"📅 {region.display_label.upper()} FORECASTS\n\nNo completed",
                )
            return format_today_forecast_from_snapshot(snapshot)

        text = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _load_forecast_text),
            timeout=FORECAST_VIEW_TIMEOUT_SECONDS,
        )
        await _replace_loading_message(
            loading_message,
            message,
            text,
            reply_markup=reply_markup,
        )
    except asyncio.TimeoutError:
        logger.warning("Forecast read timed out after %ss", FORECAST_VIEW_TIMEOUT_SECONDS)
        await _replace_loading_message(
            loading_message,
            message,
            f"📅 {region.display_label.upper()} FORECASTS\n\n"
            f"Could not load cached forecasts in time. Use {region.scan_button} to refresh.",
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logger.exception("Forecast command failed: %s", exc)
        await _replace_loading_message(
            loading_message,
            message,
            f"📅 {region.display_label.upper()} FORECASTS\n\n"
            f"Could not load cached forecasts. Use {region.scan_button} to refresh.",
            reply_markup=reply_markup,
        )


async def button_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No-op: persistent reply keyboards are removed; navigation is inline-only."""
    return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram handler error", exc_info=context.error)


def _snapshot_is_fresh(snapshot, max_age_minutes: int = 30) -> bool:
    try:
        completed = datetime.strptime(snapshot.completed_at, "%Y-%m-%d %H:%M UTC")
        completed = completed.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - completed
        return age.total_seconds() <= max_age_minutes * 60
    except Exception:
        return False


async def scheduled_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if not settings.scheduled_reports_configured:
        return
    if not settings.admin_ids:
        logger.warning("Scheduled report skipped: no ADMIN_IDS configured.")
        return

    report_timezone = load_timezone(settings.report_timezone)
    scan_window = datetime.now(report_timezone).strftime("%Y-%m-%d %H:%M")
    from app.database.storage import mark_report_sent, was_report_sent

    for region in settings.auto_report_regions:
        try:
            admin_id = settings.admin_ids[0]
            snapshot = load_latest_scan_snapshot(
                settings,
                admin_id,
                bot_data=_bot_data(context),
                region=region,
            )
            if snapshot is not None and _snapshot_is_fresh(snapshot):
                progress = snapshot.progress
            else:
                # Same safe execution path as interactive scans (deadline inside scan).
                progress = await _run_scan(settings, region)
                save_scan_snapshot(
                    settings,
                    admin_id,
                    progress,
                    bot_data=_bot_data(context),
                    region=region,
                )

            messages = format_scheduled_report(progress)
            has_forecasts = any(
                item.forecast_display
                for item in (progress.all_analyses or progress.results)
            )
            if not has_forecasts and not messages:
                continue

            for admin_id in settings.admin_ids:
                for text in messages:
                    await context.bot.send_message(chat_id=admin_id, text=text)
                for item in progress.results[:5]:
                    if was_report_sent(settings, f"{scan_window}:{region.value}", item.market_url):
                        continue
                    mark_report_sent(
                        settings,
                        f"{scan_window}:{region.value}",
                        item.market_url,
                        item.market_title,
                        item.recommendation_tier,
                    )
        except Exception as exc:
            logger.exception("Scheduled report failed for %s: %s", region.value, exc)


def schedule_report_jobs(application, settings: Settings) -> None:
    if not settings.scheduled_reports_configured:
        return
    if application.bot_data.get(SCHEDULED_REPORTS_REGISTERED_KEY):
        logger.info("Scheduled report jobs already registered; skipping duplicate setup.")
        return
    if application.job_queue is None:
        logger.warning("Job queue unavailable; scheduled reports disabled.")
        return

    tz = load_timezone(settings.report_timezone)
    for time_str in settings.report_times:
        hour, minute = map(int, time_str.split(":"))
        application.job_queue.run_daily(
            scheduled_report_job,
            time=time(hour=hour, minute=minute, tzinfo=tz),
            name=f"report_{time_str}",
        )
    application.bot_data[SCHEDULED_REPORTS_REGISTERED_KEY] = True
