"""Copy-intel polling scheduler (single JobQueue job)."""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from datetime import datetime, timezone

from app.config.settings import Settings
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.delivery import deliver_pending_alerts, retry_due_alert_deliveries
from app.copy_intel.event_identity import build_event_uid
from app.copy_intel.freshness import (
    baseline_is_complete,
    ensure_baseline_current,
    filter_alertable_events,
    run_baseline,
)
from app.copy_intel.ingestion import ingest_wallet
from app.copy_intel.paper_commit import prepare_pending_recommendation
from app.copy_intel.recommendation import build_recommendation
from app.copy_intel.risk import assert_read_only
from app.copy_intel.settings_profiles import get_user_settings
from app.copy_intel.watchlist import list_enabled, list_entries
from app.database import copy_storage

logger = logging.getLogger(__name__)

COPY_POLL_REGISTERED_KEY = "copy_intel_poll_registered"
_COPY_POLL_LOCK = "copy_intel_poll"


def _parse_iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def hard_filter_max_event_age_seconds(settings: Settings) -> int:
    """Ceiling for permanent stale marks: max of admin subscribers' ages."""
    if not settings.admin_ids:
        return int(settings.copy_max_event_age_seconds)
    ages = [
        get_user_settings(settings, admin_id).max_event_age_seconds
        for admin_id in settings.admin_ids
    ]
    return max(ages) if ages else int(settings.copy_max_event_age_seconds)


def should_skip_wallet_poll(
    settings: Settings,
    wallet: str,
    *,
    owner_id: int | None,
    now: float | None = None,
) -> bool:
    """
    Per-user poll skip (V2.2.1). Global JobQueue interval remains the execution floor;
    user interval can only make polling slower.
    """
    # Missing owner → Balanced defaults via get_user_settings (no row → Balanced).
    user = get_user_settings(settings, int(owner_id) if owner_id is not None else 0)
    interval = int(user.poll_interval_seconds)
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    if state is None or not state.last_polled_at:
        return False
    last = _parse_iso_to_epoch(state.last_polled_at)
    if last is None:
        return False
    now_ts = time.time() if now is None else float(now)
    return (now_ts - last) < interval


async def copy_intel_poll_job(context) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not settings.copy_intel_enabled:
        return
    job_started = time.monotonic()
    owner = f"poll-{uuid.uuid4().hex[:12]}"
    # Prevent overlapping poll workers from double-sending.
    lock_ttl = max(60, settings.copy_poll_interval_seconds * 2)
    if not copy_storage.try_acquire_job_lock(
        settings,
        job_name=_COPY_POLL_LOCK,
        owner=owner,
        ttl_seconds=lock_ttl,
    ):
        logger.info("Copy intel poll skipped: another poll is still running")
        return

    from app.health.jobs import finish_job_execution, record_job_execution

    record_job_execution(
        settings,
        job_name="copy_intel_poll",
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        expected_interval_seconds=settings.copy_poll_interval_seconds,
    )

    try:
        assert_read_only(settings)
        client = CopyDataClient(settings)
        entries = list_entries(settings)
        labels = {e.wallet: (e.label or e.wallet[:10]) for e in entries}
        owners = {e.wallet: e.added_by for e in entries}
        hard_age = hard_filter_max_event_age_seconds(settings)
        # (event_uid, wallet, recommendation, event_ts)
        pending_recs: list[tuple[str, str, object, int | None]] = []
        last_error = None
        stale_rejects = 0
        for wallet in list_enabled(settings):
            try:
                owner_id = owners.get(wallet)
                if should_skip_wallet_poll(settings, wallet, owner_id=owner_id):
                    logger.debug("Copy poll skip wallet %s (user interval)", wallet[:10])
                    continue

                # Outdated/missing baseline_version → MIGRATING; first cycle is baseline-only.
                ensure_baseline_current(settings, wallet)
                if not baseline_is_complete(settings, wallet):
                    status = run_baseline(settings, wallet, client=client)
                    copy_storage.touch_wallet_last_polled(settings, wallet)
                    if status != "COMPLETE":
                        last_error = f"baseline {status} for {wallet[:10]}"
                    continue

                events = ingest_wallet(settings, wallet, client=client)
                copy_storage.touch_wallet_last_polled(settings, wallet)
                alertable = filter_alertable_events(
                    settings,
                    wallet,
                    events,
                    max_age_seconds=hard_age,
                )
                stale_rejects += max(0, len(events) - len(alertable))
                owner_settings = (
                    get_user_settings(settings, owner_id)
                    if owner_id is not None
                    else get_user_settings(settings, 0)
                )
                for event in alertable:
                    if event.event_type in {"open", "increase"}:
                        rec = build_recommendation(
                            settings,
                            event,
                            trader_label=labels.get(wallet),
                            client=client,
                            min_liquidity_usd=owner_settings.min_liquidity_usd,
                        )
                        uid = event.event_uid or build_event_uid(event)
                        event_ts = int(event.ts) if event.ts is not None else None
                        # Checkpoint 8: paper write only after immutable COPY;
                        # failed paper write blocks later delivery eligibility.
                        include, paper_result = prepare_pending_recommendation(
                            settings,
                            rec,
                            condition_id=event.condition_id or "",
                            asset=event.asset or "",
                        )
                        if not include:
                            if paper_result is not None:
                                logger.warning(
                                    "Copy paper commit blocked delivery for %s: %s",
                                    uid[:24],
                                    paper_result.code,
                                )
                            continue
                        pending_recs.append((uid, wallet, rec, event_ts))
            except Exception as exc:
                last_error = str(exc)
                logger.exception("Copy intel poll failed for %s", wallet)
        copy_storage.set_poll_state(
            settings,
            "last_poll_at",
            datetime.now(timezone.utc).isoformat(),
        )
        copy_storage.set_poll_state(settings, "last_event_count", str(len(pending_recs)))
        copy_storage.set_poll_state(settings, "last_stale_rejects", str(stale_rejects))
        if pending_recs:
            copy_storage.set_poll_state(
                settings,
                "last_new_event_at",
                datetime.now(timezone.utc).isoformat(),
            )
        if last_error:
            copy_storage.set_poll_state(settings, "last_error", last_error)
        else:
            copy_storage.set_poll_state(settings, "last_error", "")

        await deliver_pending_alerts(context, settings, pending_recs)
        await retry_due_alert_deliveries(context, settings)

        finish_job_execution(
            settings,
            job_name="copy_intel_poll",
            status="ok",
            duration_ms=int((time.monotonic() - job_started) * 1000),
        )
    except Exception:
        copy_storage.set_poll_state(settings, "last_error", traceback.format_exc()[-500:])
        logger.exception("Copy intel poll job failed")
        finish_job_execution(
            settings,
            job_name="copy_intel_poll",
            status="error",
            duration_ms=int((time.monotonic() - job_started) * 1000),
            error=traceback.format_exc()[-500:],
        )
    finally:
        copy_storage.release_job_lock(settings, job_name=_COPY_POLL_LOCK, owner=owner)


def _subscriber_allows_recommendation(settings: Settings, subscriber_id: int, rec) -> bool:
    """Compatibility wrapper for tests — owner alert_mode soft filter."""
    from app.copy_intel.delivery import owner_allows_recommendation

    return owner_allows_recommendation(settings, subscriber_id, rec)


def _event_too_old_for_subscriber(
    settings: Settings,
    subscriber_id: int,
    event_ts: int | None,
    *,
    now: int | None = None,
) -> bool:
    from app.copy_intel.delivery import event_too_old_for_owner

    return event_too_old_for_owner(
        settings, subscriber_id, event_ts, now=float(now) if now is not None else None
    )


# Back-compat aliases used by characterization / settings tests.
async def _deliver_pending_alerts(context, settings, pending_recs):
    await deliver_pending_alerts(context, settings, pending_recs)


async def _retry_failed_alert_deliveries(context, settings):
    await retry_due_alert_deliveries(context, settings)


def _cards_to_messages(cards: list[str]) -> list[str]:
    from app.copy_intel.delivery import cards_to_batches

    keyed = [("w", f"u{i}", c) for i, c in enumerate(cards)]
    return [b.text for b in cards_to_batches(keyed)]


def schedule_copy_intel_jobs(application, settings: Settings) -> None:
    if not settings.copy_intel_enabled:
        return
    if application.bot_data.get(COPY_POLL_REGISTERED_KEY):
        logger.info("Copy intel poll job already registered; skipping.")
        return
    if application.job_queue is None:
        logger.warning("Job queue unavailable; copy intel polling disabled.")
        return
    interval = settings.copy_poll_interval_seconds
    application.job_queue.run_repeating(
        copy_intel_poll_job,
        interval=interval,
        first=10,
        name="copy_intel_poll",
    )
    application.bot_data[COPY_POLL_REGISTERED_KEY] = True
    logger.info("Copy intel poll job scheduled every %ss", interval)
