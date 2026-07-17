"""Copy-event freshness: baseline, watermarks, and alertable-event gating."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone

from app.config.settings import Settings
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.ingestion import parse_position, persist_snapshots
from app.copy_intel.models import TradeEvent
from app.database import copy_storage

logger = logging.getLogger(__name__)

# Explicit clock / API-index lag tolerance (seconds).
CLOCK_LAG_TOLERANCE_SECONDS = 120

_SIZE_EPS = 1e-6


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_ts(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def build_event_uid(event: TradeEvent, *, poll_seq: int = 0) -> str:
    """Stable identity: prefer tx hash; else position-diff composite."""
    if event.event_uid:
        return event.event_uid
    if event.tx_hash:
        return f"tx:{event.tx_hash.lower()}:{event.event_type}:{event.condition_id or ''}"
    size_str = f"{float(event.size or 0):.8f}"
    ts_part = str(event.ts) if event.ts is not None else "na"
    return (
        f"pd:{event.wallet}|{event.condition_id or ''}|"
        f"{event.event_type}|{size_str}|{event.side or ''}|"
        f"{ts_part}|{poll_seq}"
    )


def begin_subscription(settings: Settings, wallet: str) -> None:
    """Start a new subscription cycle (add or re-enable). No alerts until baseline completes."""
    now = _now_iso()
    copy_storage.upsert_wallet_monitor_state(
        settings,
        wallet,
        subscription_started_at=now,
        baseline_started_at=None,
        baseline_status="PENDING",
        baseline_error=None,
        newest_event_ts=None,
        newest_event_uid=None,
        monitoring_enabled=True,
    )
    copy_storage.upsert_wallet_event_watermark(
        settings,
        wallet,
        newest_event_ts=None,
        newest_event_uid=None,
    )
    logger.info("Copy monitor subscription started for %s (baseline PENDING)", wallet)


def ensure_monitor_state(settings: Settings, wallet: str) -> copy_storage.WalletMonitorState:
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    if state is None:
        begin_subscription(settings, wallet)
        state = copy_storage.get_wallet_monitor_state(settings, wallet)
        assert state is not None
    return state


def baseline_is_complete(settings: Settings, wallet: str) -> bool:
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    return state is not None and state.baseline_status == "COMPLETE"


def run_baseline(settings: Settings, wallet: str, client: CopyDataClient | None = None) -> str:
    """
    Fetch current positions solely to initialize baseline.
    Marks pre-existing book as seen; sends zero alerts.
    Returns baseline_status after the attempt.
    """
    client = client or CopyDataClient(settings)
    state = ensure_monitor_state(settings, wallet)
    if state.baseline_status == "COMPLETE":
        return "COMPLETE"

    now_iso = _now_iso()
    observed_at = int(time.time())
    copy_storage.update_wallet_monitor_fields(
        settings,
        wallet,
        baseline_started_at=state.baseline_started_at or now_iso,
        baseline_status="RUNNING",
        baseline_error=None,
    )
    run_id = copy_storage.start_baseline_run(settings, wallet)
    try:
        raw_positions = client.fetch_positions(wallet)
        current = {
            p.condition_id: p
            for p in (parse_position(item, wallet) for item in raw_positions)
            if p.condition_id and p.size > _SIZE_EPS
        }
        # Replace snapshots so downtime diffs never become alerts on re-enable.
        copy_storage.clear_position_snapshots(settings, wallet)
        persist_snapshots(settings, current)

        # Watermark floor is subscription start minus lag (not wall-clock now) so
        # delayed API timestamps remain eligible; baseline IDs are separate.
        state = copy_storage.get_wallet_monitor_state(settings, wallet)
        assert state is not None
        newest_ts = _parse_iso_ts(state.subscription_started_at) - CLOCK_LAG_TOLERANCE_SECONDS
        newest_uid: str | None = None
        for idx, snap in enumerate(sorted(current.values(), key=lambda s: s.condition_id)):
            uid = f"baseline:{wallet}|{snap.condition_id}|{snap.size:.8f}|{idx}"
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=observed_at,
                observed_at=observed_at,
                is_baseline=True,
                alertable=False,
                reject_reason="baseline",
            )
            newest_uid = uid

        copy_storage.upsert_wallet_event_watermark(
            settings,
            wallet,
            newest_event_ts=newest_ts,
            newest_event_uid=newest_uid,
        )
        copy_storage.update_wallet_monitor_fields(
            settings,
            wallet,
            baseline_status="COMPLETE",
            baseline_error=None,
            newest_event_ts=newest_ts,
            newest_event_uid=newest_uid,
        )
        copy_storage.finish_baseline_run(
            settings,
            run_id,
            status="COMPLETE",
            positions_count=len(current),
        )
        logger.info(
            "Baseline complete for %s (%s positions); alerts enabled for subsequent events",
            wallet,
            len(current),
        )
        return "COMPLETE"
    except Exception as exc:
        err = str(exc)[:500]
        copy_storage.update_wallet_monitor_fields(
            settings,
            wallet,
            baseline_status="FAILED",
            baseline_error=err,
        )
        copy_storage.finish_baseline_run(
            settings,
            run_id,
            status="FAILED",
            error=err,
        )
        logger.exception("Baseline failed for %s", wallet)
        return "FAILED"


def annotate_events(
    events: list[TradeEvent],
    *,
    observed_at: int | None = None,
) -> list[TradeEvent]:
    observed = observed_at if observed_at is not None else int(time.time())
    annotated: list[TradeEvent] = []
    for idx, event in enumerate(events):
        uid = build_event_uid(event, poll_seq=idx)
        annotated.append(
            replace(
                event,
                observed_at=event.observed_at if event.observed_at is not None else observed,
                event_uid=uid,
            )
        )
    return annotated


def filter_alertable_events(
    settings: Settings,
    wallet: str,
    events: list[TradeEvent],
    *,
    now: int | None = None,
    market_resolved: dict[str, bool] | None = None,
) -> list[TradeEvent]:
    """
    Return only genuinely new, actionable events.
    Always records processed markers (dedup / reject reasons).
    """
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    if state is None or state.baseline_status != "COMPLETE":
        return []

    now_ts = now if now is not None else int(time.time())
    sub_start = _parse_iso_ts(state.subscription_started_at)
    watermark_ts = state.newest_event_ts
    lag = CLOCK_LAG_TOLERANCE_SECONDS
    max_age = settings.copy_max_event_age_seconds
    resolved_map = market_resolved or {}

    alertable: list[TradeEvent] = []
    newest_ts = watermark_ts
    newest_uid = state.newest_event_uid

    for event in annotate_events(events, observed_at=now_ts):
        uid = event.event_uid or build_event_uid(event)
        observed_at = event.observed_at if event.observed_at is not None else now_ts
        event_ts = int(event.ts) if event.ts is not None else observed_at

        if copy_storage.is_processed_copy_event(settings, uid):
            continue

        if event.ts is not None and event.ts < (sub_start - lag):
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=event_ts,
                observed_at=observed_at,
                alertable=False,
                reject_reason="before_subscription",
            )
            continue

        # Strictly older than watermark is out-of-order / historical.
        # Equal timestamps with new UIDs remain eligible.
        if (
            watermark_ts is not None
            and event.ts is not None
            and event.ts < watermark_ts
        ):
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=event_ts,
                observed_at=observed_at,
                alertable=False,
                reject_reason="before_watermark",
            )
            continue

        age = now_ts - event_ts
        if age > max_age:
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=event_ts,
                observed_at=observed_at,
                alertable=False,
                reject_reason="stale_age",
            )
            continue

        condition_id = event.condition_id or ""
        if condition_id and resolved_map.get(condition_id):
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=event_ts,
                observed_at=observed_at,
                alertable=False,
                reject_reason="market_resolved",
            )
            continue

        inserted = copy_storage.mark_processed_copy_event(
            settings,
            event_uid=uid,
            wallet=wallet,
            event_ts=event_ts,
            observed_at=observed_at,
            alertable=True,
            reject_reason=None,
        )
        if not inserted:
            continue

        if newest_ts is None or event_ts >= newest_ts:
            newest_ts = event_ts
            newest_uid = uid
        alertable.append(event)

    if newest_ts != watermark_ts or newest_uid != state.newest_event_uid:
        copy_storage.upsert_wallet_event_watermark(
            settings,
            wallet,
            newest_event_ts=newest_ts,
            newest_event_uid=newest_uid,
        )
        copy_storage.update_wallet_monitor_fields(
            settings,
            wallet,
            newest_event_ts=newest_ts,
            newest_event_uid=newest_uid,
        )

    return alertable
