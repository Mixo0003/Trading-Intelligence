import asyncio
import logging
from datetime import datetime, time, timezone
from functools import partial

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.config.timezones import load_timezone
from app.database.storage import get_recent_analyses, upsert_user
from app.polymarket.client import PolymarketClient
from app.region.session import get_user_scan_region, set_user_scan_region
from app.region.types import ScanRegion
from app.signals.scan_service import analyze_market_by_url, scan_weather_markets
from app.telegram.buttons import (
    BTN_BACK,
    BTN_BEST_OPPORTUNITIES,
    BTN_CHANGE_REGION,
    BTN_FORECAST,
    BTN_HELP,
    BTN_RECENT,
    BTN_STATUS,
    BTN_TOP_SIGNALS,
    BTN_WEATHER,
    REGION_BUTTON_TO_SCAN_REGION,
    is_regional_action_button,
    main_menu_keyboard,
    region_menu_keyboard,
    regional_action_keyboard,
)
from app.telegram.formatting import (
    format_concise_opportunity,
    format_opportunities_bundle,
    format_scan_report,
    format_scheduled_report,
    format_today_forecast_from_snapshot,
    format_top_opportunities,
    format_top_signals_report,
    format_top_signals_unavailable,
    persist_scan_results,
)
from app.telegram.scan_cache import load_latest_scan_snapshot, save_scan_snapshot
from app.telegram.messages import HELP_TEXT, START_TEXT, WEATHER_REGION_TEXT

logger = logging.getLogger(__name__)

TOP_SIGNALS_TIMEOUT_SECONDS = 5
FORECAST_VIEW_TIMEOUT_SECONDS = 5
REGIONAL_SCAN_TIMEOUT_SECONDS = 180
SCHEDULED_REPORTS_REGISTERED_KEY = "scheduled_reports_registered"


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _bot_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data


async def _run_scan(settings: Settings, region: ScanRegion):
    loop = asyncio.get_running_loop()
    fn = partial(scan_weather_markets, settings, region=region)
    return await loop.run_in_executor(None, fn)


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
    return regional_action_keyboard(region)


async def _replace_loading_message(
    loading_message,
    source_message,
    final_text: str,
    *,
    reply_markup,
) -> None:
    """Replace a progress message, deleting and replying only if editing fails."""
    try:
        # Telegram cannot attach a ReplyKeyboardMarkup to an edited message.
        # The persistent keyboard sent with the loading message remains active.
        await loading_message.edit_text(final_text)
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
    if update.message:
        await update.message.reply_text(START_TEXT, reply_markup=main_menu_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Main menu restored.",
            reply_markup=main_menu_keyboard(),
        )


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            WEATHER_REGION_TEXT,
            reply_markup=region_menu_keyboard(),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_TEXT, reply_markup=main_menu_keyboard())


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
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    region = get_user_scan_region(context)
    await _track_user(update, settings)
    message = update.message or update.effective_message
    if message is None:
        return
    reply_markup = _reply_markup_for_region(region)
    loading_message = await message.reply_text(
        f"🔍 Scanning {region.display_label} Polymarket weather markets...",
        reply_markup=reply_markup,
    )
    try:
        progress = await asyncio.wait_for(
            _run_scan(settings, region),
            timeout=REGIONAL_SCAN_TIMEOUT_SECONDS,
        )
        user = update.effective_user
        if user:
            persist_scan_results(settings, user.id, progress)
            save_scan_snapshot(
                settings,
                user.id,
                progress,
                bot_data=_bot_data(context),
                region=region,
            )
        await _replace_loading_message(
            loading_message,
            message,
            format_scan_report(progress),
            reply_markup=reply_markup,
        )
    except asyncio.TimeoutError:
        logger.warning("Regional scan timed out after %ss", REGIONAL_SCAN_TIMEOUT_SECONDS)
        await _replace_loading_message(
            loading_message,
            message,
            f"❌ {region.display_label} scan timed out. Partial results may be unavailable.",
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logger.exception("Scan command failed: %s", exc)
        await _replace_loading_message(
            loading_message,
            message,
            "❌ Scan failed due to a provider error. Please retry shortly.",
            reply_markup=reply_markup,
        )


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    if message is None:
        return
    region = get_user_scan_region(context)
    telegram_id = update.effective_user.id if update.effective_user else message.chat.id
    reply_markup = main_menu_keyboard()
    loading_message = await message.reply_text(
        f"📊 Loading {region.display_label} opportunities...",
        reply_markup=reply_markup,
    )
    try:
        settings = _settings(context)
        snapshot = load_latest_scan_snapshot(
            settings,
            telegram_id,
            bot_data=_bot_data(context),
            region=region,
        )
        if snapshot is None:
            text = format_top_signals_unavailable(region)
        else:
            text = format_top_opportunities(snapshot.progress)
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
            f"❌ Could not load opportunities. Use {region.scan_button} first.",
            reply_markup=reply_markup,
        )


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    message = update.message
    if message is None:
        return
    if not context.args:
        await message.reply_text(
            "Usage: /analyze https://polymarket.com/event/...\n\nOptional manual fallback only.",
            reply_markup=main_menu_keyboard(),
        )
        return
    url = context.args[0]
    reply_markup = main_menu_keyboard()
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
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())


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
    message = update.message
    if message is None or not message.text:
        return

    text = message.text.strip()
    region = get_user_scan_region(context)

    if text == BTN_WEATHER:
        await weather_command(update, context)
    elif text == BTN_BACK:
        await menu_command(update, context)
    elif text == BTN_CHANGE_REGION:
        await weather_command(update, context)
    elif text in REGION_BUTTON_TO_SCAN_REGION:
        selected = REGION_BUTTON_TO_SCAN_REGION[text]
        set_user_scan_region(context, selected)
        if message:
            await message.reply_text(
                f"Region set to {selected.display_label}.",
                reply_markup=regional_action_keyboard(selected),
            )
    elif text == region.scan_button:
        await scan_command(update, context)
    elif text == BTN_TOP_SIGNALS:
        user = update.effective_user
        await _reply_top_signals(
            message,
            context,
            telegram_id=user.id if user else None,
            region=region,
            reply_markup=_reply_markup_for_region(region),
        )
    elif text == BTN_FORECAST:
        await forecast_command(update, context)
    elif text == BTN_BEST_OPPORTUNITIES:
        await top_command(update, context)
    elif text == BTN_RECENT:
        await recent_command(update, context)
    elif text == BTN_STATUS:
        await status_command(update, context)
    elif text == BTN_HELP:
        await help_command(update, context)
    elif is_regional_action_button(text, region):
        if text == region.scan_button:
            await scan_command(update, context)
        elif text == BTN_TOP_SIGNALS:
            user = update.effective_user
            await _reply_top_signals(
                message,
                context,
                telegram_id=user.id if user else None,
                region=region,
                reply_markup=_reply_markup_for_region(region),
            )
        elif text == BTN_FORECAST:
            await forecast_command(update, context)
        elif text == BTN_CHANGE_REGION:
            await weather_command(update, context)


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
                progress = await asyncio.wait_for(
                    _run_scan(settings, region),
                    timeout=REGIONAL_SCAN_TIMEOUT_SECONDS,
                )
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
