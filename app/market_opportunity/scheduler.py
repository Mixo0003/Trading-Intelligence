"""JobQueue registration for Market Opportunity Intelligence (V4.2 WTI)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from app.config.settings import Settings
from app.database import copy_storage
from app.market_opportunity.detectors.registry import (
    default_registry,
    enabled_detectors,
    register_wti_detectors,
)
from app.market_opportunity.paper import assert_paper_only
from app.market_opportunity.settings import module_enabled
from app.market_opportunity.sources.eia import DailyBar, InventoryPoint

logger = logging.getLogger(__name__)

MOI_JOBS_REGISTERED_KEY = "market_opportunity_jobs_registered"
MOI_CACHE_KEY = "moi_wti_cache"
MOI_LOCK_NAME = "moi_wti_jobs"

MOI_PRICE_REFRESH_JOB = "moi_wti_price_refresh"
MOI_INVENTORY_REFRESH_JOB = "moi_wti_inventory_refresh"
MOI_SCAN_JOB = "moi_wti_scan"
MOI_OUTCOMES_JOB = "moi_wti_outcomes"

# Intervals (seconds) — source-driven cadence with modest polling.
_PRICE_REFRESH_INTERVAL = 6 * 3600
_INVENTORY_REFRESH_INTERVAL = 12 * 3600
_SCAN_INTERVAL = 3600
_OUTCOMES_INTERVAL = 6 * 3600
_LOCK_TTL = 15 * 60


def compute_wti_data_hash(
    bars: Sequence[DailyBar] | None,
    inventory: Sequence[InventoryPoint] | None = None,
) -> str:
    """Stable hash of latest price + inventory observations for change detection."""
    parts: list[str] = []
    if bars:
        last = bars[-1]
        parts.append(f"p:{last.period.isoformat()}:{last.close}")
    if inventory:
        last_i = inventory[-1]
        parts.append(f"i:{last_i.period.isoformat()}:{last_i.value}")
    if not parts:
        return ""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest


def data_hash_changed(previous: str | None, current: str) -> bool:
    """True when ``current`` is non-empty and differs from ``previous``."""
    if not current:
        return False
    return previous != current


def _cache(bot_data: dict[str, Any]) -> dict[str, Any]:
    cache = bot_data.setdefault(MOI_CACHE_KEY, {})
    if not isinstance(cache, dict):
        cache = {}
        bot_data[MOI_CACHE_KEY] = cache
    return cache


def _try_lock(settings: Settings, owner: str) -> bool:
    return copy_storage.try_acquire_job_lock(
        settings,
        job_name=MOI_LOCK_NAME,
        owner=owner,
        ttl_seconds=_LOCK_TTL,
    )


def _release_lock(settings: Settings, owner: str) -> None:
    try:
        copy_storage.release_job_lock(
            settings, job_name=MOI_LOCK_NAME, owner=owner
        )
    except Exception:
        logger.debug("MOI lock release skipped", exc_info=True)


def _write_runtime(
    settings: Settings,
    *,
    status: str,
    detail: dict[str, Any] | str,
) -> None:
    """Persist runtime status, merging dict details so jobs do not wipe peers.

    Price/inventory/FRED/detector/outcome writers each update a slice of the
    same row. Replacing the whole detail blob made System Health falsely report
    EIA RWTC missing after outcomes/idle jobs.
    """
    from app.database import market_opportunity_storage as moi_storage

    if isinstance(detail, str):
        text = detail
    else:
        merged: dict[str, Any] = {}
        try:
            prev = moi_storage.get_runtime_status(settings)
            prev_detail = (prev or {}).get("detail")
            if isinstance(prev_detail, dict):
                merged.update(prev_detail)
        except Exception:
            logger.debug("MOI runtime merge read skipped", exc_info=True)
        merged.update(detail)
        text = json.dumps(merged, default=str)
    try:
        moi_storage.upsert_runtime_status(
            settings,
            status=status,
            detail=text,
            checked_at=datetime.now(timezone.utc),
            enabled_detectors=list(enabled_detectors()),
        )
    except Exception:
        logger.debug("MOI runtime status update skipped", exc_info=True)


async def moi_wti_price_refresh_job(context) -> None:
    """Fetch EIA RWTC (+ optional FRED). No Telegram alerts."""
    settings: Settings = context.application.bot_data["settings"]
    if not module_enabled(settings):
        return
    assert_paper_only(settings)
    owner = f"moi-price-{uuid.uuid4().hex[:10]}"
    if not _try_lock(settings, owner):
        logger.info("MOI price refresh skipped: lock held")
        return
    try:
        api_key = (getattr(settings, "eia_api_key", None) or os.getenv("EIA_API_KEY", "")).strip()
        if not api_key:
            _write_runtime(
                settings,
                status="degraded",
                detail={"wti_price": "missing_eia_api_key"},
            )
            return
        from app.market_opportunity.sources.eia import fetch_rwtc_daily

        bars = fetch_rwtc_daily(api_key, length=120)
        cache = _cache(context.application.bot_data)
        cache["bars"] = bars
        cache["price_fetched_at"] = datetime.now(timezone.utc)
        cache["price_period"] = bars[-1].period.isoformat() if bars else None

        fred_key = (getattr(settings, "fred_api_key", None) or os.getenv("FRED_API_KEY", "")).strip()
        if fred_key:
            try:
                from app.market_opportunity.sources.fred import fetch_dcoilwtico

                cache["fred"] = fetch_dcoilwtico(fred_key, limit=120)
                cache["fred_ok"] = True
            except Exception as exc:
                cache["fred"] = []
                cache["fred_ok"] = False
                cache["fred_error"] = type(exc).__name__
        else:
            cache["fred"] = []
            cache["fred_ok"] = False
            cache["fred_error"] = "missing_key"

        data_hash = compute_wti_data_hash(bars, cache.get("inventory"))
        cache["data_hash_current"] = data_hash
        _write_runtime(
            settings,
            status="ok",
            detail={
                "job": MOI_PRICE_REFRESH_JOB,
                "wti_price": {
                    "period": cache.get("price_period"),
                    "bars": len(bars),
                },
                "fred_crosscheck": {
                    "ok": cache.get("fred_ok"),
                    "error": cache.get("fred_error"),
                },
                "data_hash": data_hash,
            },
        )
    except Exception as exc:
        logger.exception("MOI price refresh failed")
        _write_runtime(
            settings,
            status="down",
            detail={"job": MOI_PRICE_REFRESH_JOB, "error": type(exc).__name__},
        )
    finally:
        _release_lock(settings, owner)


async def moi_wti_inventory_refresh_job(context) -> None:
    """Fetch EIA WCESTUS1 weekly inventory. No Telegram alerts."""
    settings: Settings = context.application.bot_data["settings"]
    if not module_enabled(settings):
        return
    assert_paper_only(settings)
    owner = f"moi-inv-{uuid.uuid4().hex[:10]}"
    if not _try_lock(settings, owner):
        logger.info("MOI inventory refresh skipped: lock held")
        return
    try:
        api_key = (getattr(settings, "eia_api_key", None) or os.getenv("EIA_API_KEY", "")).strip()
        if not api_key:
            _write_runtime(
                settings,
                status="degraded",
                detail={"wti_inventory": "missing_eia_api_key"},
            )
            return
        from app.market_opportunity.sources.eia import fetch_inventory_wcestus1

        points = fetch_inventory_wcestus1(api_key, length=40)
        cache = _cache(context.application.bot_data)
        cache["inventory"] = points
        cache["inventory_fetched_at"] = datetime.now(timezone.utc)
        cache["inventory_period"] = points[-1].period.isoformat() if points else None
        data_hash = compute_wti_data_hash(cache.get("bars"), points)
        cache["data_hash_current"] = data_hash
        _write_runtime(
            settings,
            status="ok",
            detail={
                "job": MOI_INVENTORY_REFRESH_JOB,
                "wti_inventory": {
                    "period": cache.get("inventory_period"),
                    "points": len(points),
                },
                "data_hash": data_hash,
            },
        )
    except Exception as exc:
        logger.exception("MOI inventory refresh failed")
        _write_runtime(
            settings,
            status="degraded",
            detail={
                "job": MOI_INVENTORY_REFRESH_JOB,
                "error": type(exc).__name__,
            },
        )
    finally:
        _release_lock(settings, owner)


async def moi_wti_scan_job(context) -> None:
    """Stage A+B scan when data hash changed. No Telegram alerts."""
    settings: Settings = context.application.bot_data["settings"]
    if not module_enabled(settings):
        return
    assert_paper_only(settings)
    owner = f"moi-scan-{uuid.uuid4().hex[:10]}"
    if not _try_lock(settings, owner):
        logger.info("MOI scan skipped: lock held")
        return
    try:
        cache = _cache(context.application.bot_data)
        bars = cache.get("bars") or []
        inventory = cache.get("inventory") or []
        current = compute_wti_data_hash(bars, inventory)
        previous = cache.get("data_hash_scanned")
        if not data_hash_changed(previous, current):
            _write_runtime(
                settings,
                status="idle",
                detail={
                    "job": MOI_SCAN_JOB,
                    "skipped": "unchanged_hash",
                    "data_hash": current,
                },
            )
            return
        if not bars:
            _write_runtime(
                settings,
                status="degraded",
                detail={"job": MOI_SCAN_JOB, "skipped": "no_bars"},
            )
            return

        from app.market_opportunity.pipeline import WtiPipelineInput, run_wti_scan

        fetched_at = cache.get("price_fetched_at") or datetime.now(timezone.utc)
        result = run_wti_scan(
            settings,
            WtiPipelineInput(
                bars=bars,
                inventory=inventory,
                fred=cache.get("fred") or [],
                fetched_at=fetched_at,
                now=datetime.now(timezone.utc),
            ),
            persist=True,
        )
        cache["data_hash_scanned"] = current
        cache["last_scan_ok"] = True
        cache["last_scan_published"] = len(result.published)
        _write_runtime(
            settings,
            status="ok",
            detail={
                "job": MOI_SCAN_JOB,
                "detector_runtime": {
                    "ok": True,
                    "stage_a": result.stage_a_count,
                    "published": len(result.published),
                    "suppressed": len(result.suppressed),
                },
                "data_hash": current,
                "diagnostics": result.diagnostics,
            },
        )
    except Exception as exc:
        logger.exception("MOI scan failed")
        cache = _cache(context.application.bot_data)
        cache["last_scan_ok"] = False
        _write_runtime(
            settings,
            status="down",
            detail={
                "job": MOI_SCAN_JOB,
                "detector_runtime": {"ok": False, "error": type(exc).__name__},
            },
        )
    finally:
        _release_lock(settings, owner)


async def moi_wti_outcomes_job(context) -> None:
    """Measure paper outcomes after new daily bars. No Telegram alerts."""
    settings: Settings = context.application.bot_data["settings"]
    if not module_enabled(settings):
        return
    assert_paper_only(settings)
    owner = f"moi-out-{uuid.uuid4().hex[:10]}"
    if not _try_lock(settings, owner):
        logger.info("MOI outcomes skipped: lock held")
        return
    try:
        cache = _cache(context.application.bot_data)
        bars = cache.get("bars") or []
        if not bars:
            _write_runtime(
                settings,
                status="degraded",
                detail={"job": MOI_OUTCOMES_JOB, "skipped": "no_bars"},
            )
            return
        last_measured_period = cache.get("outcomes_bar_period")
        newest = bars[-1].period.isoformat()
        if last_measured_period == newest:
            _write_runtime(
                settings,
                status="idle",
                detail={
                    "job": MOI_OUTCOMES_JOB,
                    "skipped": "no_new_daily_bar",
                    "period": newest,
                },
            )
            return

        from app.market_opportunity.outcomes import measure_open_opportunities

        outcomes = measure_open_opportunities(
            settings,
            bars,
            measured_at=datetime.now(timezone.utc),
            persist=True,
        )
        cache["outcomes_bar_period"] = newest
        cache["last_outcome_ok"] = True
        _write_runtime(
            settings,
            status="ok",
            detail={
                "job": MOI_OUTCOMES_JOB,
                "paper_outcome_runtime": {
                    "ok": True,
                    "measured": len(outcomes),
                    "period": newest,
                },
            },
        )
    except Exception as exc:
        logger.exception("MOI outcomes failed")
        cache = _cache(context.application.bot_data)
        cache["last_outcome_ok"] = False
        _write_runtime(
            settings,
            status="down",
            detail={
                "job": MOI_OUTCOMES_JOB,
                "paper_outcome_runtime": {
                    "ok": False,
                    "error": type(exc).__name__,
                },
            },
        )
    finally:
        _release_lock(settings, owner)


def schedule_market_opportunity_jobs(application, settings: Settings) -> None:
    """Idempotent. Disabled module registers nothing.

    When enabled: register WTI refresh/scan/outcome jobs with concurrency lock=1.
    Detectors are registered with enabled=False by default; pipeline calls classes
    directly. Never registers Gold. Never sends Telegram alerts.
    """
    if not module_enabled(settings):
        logger.info("Market Opportunity Intelligence disabled; jobs not registered")
        return
    if application.bot_data.get(MOI_JOBS_REGISTERED_KEY):
        logger.info("Market Opportunity jobs already registered; skipping")
        return
    if application.job_queue is None:
        logger.warning("Job queue unavailable; Market Opportunity jobs not scheduled")
        return

    assert_paper_only(settings)
    register_wti_detectors(default_registry, enabled=False)

    application.job_queue.run_repeating(
        moi_wti_price_refresh_job,
        interval=_PRICE_REFRESH_INTERVAL,
        first=90,
        name=MOI_PRICE_REFRESH_JOB,
    )
    application.job_queue.run_repeating(
        moi_wti_inventory_refresh_job,
        interval=_INVENTORY_REFRESH_INTERVAL,
        first=120,
        name=MOI_INVENTORY_REFRESH_JOB,
    )
    application.job_queue.run_repeating(
        moi_wti_scan_job,
        interval=_SCAN_INTERVAL,
        first=180,
        name=MOI_SCAN_JOB,
    )
    application.job_queue.run_repeating(
        moi_wti_outcomes_job,
        interval=_OUTCOMES_INTERVAL,
        first=240,
        name=MOI_OUTCOMES_JOB,
    )
    application.bot_data[MOI_JOBS_REGISTERED_KEY] = True
    logger.info(
        "Market Opportunity V4.2 WTI jobs scheduled "
        "(refresh/scan/outcomes; no alerts; detectors registered disabled)"
    )
