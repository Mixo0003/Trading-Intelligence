"""Isolated Live Edge JobQueue. Does not start a Telegram poller."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from app.config.settings import Settings
from app.database import copy_storage, live_edge_storage
from app.opportunity_intel.live_edge.scanner import live_edge_users, run_live_edge_cycle
from app.opportunity_intel.models import AlertMode

logger = logging.getLogger(__name__)

LIVE_EDGE_SCAN_REGISTERED_KEY = "live_edge_scan_registered"
LIVE_EDGE_SCAN_JOB_NAME = "live_edge_scan_job"
_LIVE_EDGE_LOCK = "live_edge_scan"
_SHUTDOWN_TASKS_KEY = "live_edge_scan_background_tasks"


def _run_cycle(settings: Settings):
    from app.opportunity_intel.book import PublicOrderBookClient

    return run_live_edge_cycle(settings, book_client=PublicOrderBookClient(settings))


def _lock_ttl(settings: Settings) -> int:
    return max(120, int(settings.opportunity_scan_interval_seconds) * 2)


def _has_inflight_live_edge(application) -> bool:
    tasks = application.bot_data.get(_SHUTDOWN_TASKS_KEY) or set()
    return any(not task.done() for task in tasks)


async def _renew_lock_loop(
    settings: Settings,
    *,
    owner: str,
    stop: asyncio.Event,
) -> None:
    ttl = _lock_ttl(settings)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(5.0, ttl / 3))
            return
        except asyncio.TimeoutError:
            try:
                copy_storage.renew_job_lock(
                    settings,
                    job_name=_LIVE_EDGE_LOCK,
                    owner=owner,
                    ttl_seconds=ttl,
                )
            except sqlite3.OperationalError:
                logger.info("Live Edge lock renew skipped: sqlite busy")


def _track_task(application, task: asyncio.Task) -> None:
    tasks: set[asyncio.Task] = application.bot_data.setdefault(_SHUTDOWN_TASKS_KEY, set())
    tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Live Edge background task failed")

    task.add_done_callback(_done)


async def live_edge_scan_job(context) -> None:
    settings: Settings = context.application.bot_data["settings"]
    try:
        if not live_edge_users(settings):
            return
    except sqlite3.OperationalError:
        return
    if _has_inflight_live_edge(context.application):
        logger.info("Live Edge scan skipped: overlap protection")
        return
    owner = f"le-{uuid.uuid4().hex[:12]}"
    ttl = _lock_ttl(settings)
    if not copy_storage.try_acquire_job_lock(
        settings, job_name=_LIVE_EDGE_LOCK, owner=owner, ttl_seconds=ttl
    ):
        logger.info("Live Edge scan skipped: overlap protection")
        return

    async def _work() -> None:
        stop_renew = asyncio.Event()
        renew_task = asyncio.create_task(
            _renew_lock_loop(settings, owner=owner, stop=stop_renew),
            name="live_edge_scan_lock_renew",
        )
        try:
            result = await asyncio.to_thread(_run_cycle, settings)
            stop_renew.set()
            logger.info(
                "Live Edge cycle users=%s markets=%s evaluated=%s buy=%s wait=%s watch=%s too_late=%s skip=%s funnel=%s",
                result.enabled_users,
                result.markets_discovered,
                result.evaluations,
                result.buy_now,
                result.wait,
                result.watch,
                result.too_late,
                result.skipped,
                result.funnel,
            )
            bot = getattr(context, "bot", None)
            send = getattr(bot, "send_message", None)
            if callable(send):
                delivered = await deliver_live_edge_alerts(
                    settings, result.wait_fires, send
                )
                logger.info("Live Edge delivery sent=%s", delivered)
        except Exception:
            logger.exception("Live Edge scan job failed")
        finally:
            stop_renew.set()
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
            except sqlite3.OperationalError:
                logger.info("Live Edge lock renew skipped: sqlite busy")
            copy_storage.release_job_lock(
                settings, job_name=_LIVE_EDGE_LOCK, owner=owner
            )

    task = asyncio.create_task(_work(), name=LIVE_EDGE_SCAN_JOB_NAME)
    _track_task(context.application, task)


def schedule_live_edge_jobs(application, settings: Settings) -> None:
    """Idempotent registration. Skips when no user has Live Edge ON."""
    if application.bot_data.get(LIVE_EDGE_SCAN_REGISTERED_KEY):
        logger.info("Live Edge scan job already registered; skipping.")
        return
    try:
        enabled = bool(live_edge_users(settings))
    except sqlite3.OperationalError:
        logger.info("Live Edge schema not ready; scan job not registered")
        return
    if not enabled:
        logger.info("Live Edge unarmed (no enabled users); scan job not registered")
        return
    if application.job_queue is None:
        logger.warning("Job queue unavailable; Live Edge scanner not scheduled.")
        return
    interval = max(30, int(settings.opportunity_scan_interval_seconds))
    application.job_queue.run_repeating(
        live_edge_scan_job,
        interval=interval,
        first=min(30, interval),
        name=LIVE_EDGE_SCAN_JOB_NAME,
    )
    application.bot_data[LIVE_EDGE_SCAN_REGISTERED_KEY] = True
    application.bot_data.setdefault(_SHUTDOWN_TASKS_KEY, set())
    logger.info("Live Edge scan job scheduled every %ss", interval)


async def deliver_live_edge_alerts(settings: Settings, wait_fires, send) -> int:
    """Push WAIT-trigger fires, first BUY_NOW/WAIT cards, then capped WATCH notices."""
    from app.opportunity_intel.live_edge.catalog import format_stored_card
    from app.opportunity_intel.live_edge.formatting import (
        _card_payload_dict,
        format_watch_telegram,
        polymarket_open_lines,
    )

    sent = 0
    watch_sent = 0
    clock = datetime.now(timezone.utc)
    for fire in wait_fires:
        if live_edge_storage.already_delivered(
            settings,
            signal_uid=fire.signal_uid,
            telegram_id=fire.telegram_id,
            kind="wait_trigger",
        ):
            continue
        text = "\n".join(
            [
                "🚨 УМОВА ДЛЯ ВХОДУ ВИКОНАНА",
                fire.question,
                "Зайти, якщо:",
                f"🎯 {fire.trigger_description}",
            ]
        )
        stored = live_edge_storage.load_signal(settings, fire.signal_uid)
        if stored is not None:
            extra = polymarket_open_lines(payload=_card_payload_dict(stored))
            if extra:
                text = text + "\n" + "\n".join(extra)
        await send(chat_id=fire.telegram_id, text=text[:4096])
        live_edge_storage.record_delivery(
            settings,
            signal_uid=fire.signal_uid,
            telegram_id=fire.telegram_id,
            kind="wait_trigger",
            delivered_at=clock,
        )
        sent += 1
    for telegram_id, user_settings in live_edge_users(settings):
        if user_settings.alert_mode != AlertMode.INSTANT:
            continue
        rows = live_edge_storage.list_active_signals(
            settings, telegram_id=telegram_id, limit=40
        )
        for row in rows:
            if row.decision not in {"BUY_NOW", "WAIT"}:
                continue
            if live_edge_storage.already_delivered(
                settings,
                signal_uid=row.signal_uid,
                telegram_id=telegram_id,
                kind="alert",
            ):
                continue
            await send(chat_id=telegram_id, text=format_stored_card(row)[:4096])
            live_edge_storage.record_delivery(
                settings,
                signal_uid=row.signal_uid,
                telegram_id=telegram_id,
                kind="alert",
                delivered_at=clock,
            )
            sent += 1
        watch_budget = live_edge_storage.WATCH_MAX_PER_WINDOW - (
            live_edge_storage.count_watch_deliveries_since(
                settings,
                telegram_id=telegram_id,
                since=clock - timedelta(seconds=live_edge_storage.WATCH_WINDOW_SECONDS),
            )
        )
        for row in rows:
            if watch_budget <= 0:
                break
            if row.decision != "WATCH":
                continue
            if not _useful_watch_row(row):
                continue
            fingerprint = _watch_row_fingerprint(row)
            if live_edge_storage.watch_already_sent(
                settings,
                signal_uid=row.signal_uid,
                telegram_id=telegram_id,
                fingerprint=fingerprint,
            ):
                continue
            await send(chat_id=telegram_id, text=format_watch_telegram(row)[:4096])
            live_edge_storage.record_delivery(
                settings,
                signal_uid=row.signal_uid,
                telegram_id=telegram_id,
                kind=f"{live_edge_storage.WATCH_DELIVERY_KIND_PREFIX}{fingerprint}",
                delivered_at=clock,
            )
            sent += 1
            watch_sent += 1
            watch_budget -= 1
    logger.info(
        "Live Edge delivery sent=%s watch=%s",
        sent,
        watch_sent,
    )
    return sent


def _useful_watch_row(row) -> bool:
    question = (row.question or "").lower()
    if any(
        needle in question
        for needle in ("exact score", "first corner", "odd/even", "total kills")
    ):
        return False
    try:
        payload = json.loads(row.card_json or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("has_live_state") is True


def _watch_row_fingerprint(row) -> str:
    try:
        payload = json.loads(row.card_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict) and payload.get("watch_fingerprint"):
        return str(payload["watch_fingerprint"])[:32]
    return "default"
