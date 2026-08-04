"""Deterministic health probes for Telegram, scheduler, weather, copy, infrastructure."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import Settings
from app.database import copy_storage
from app.database.storage import get_connection
from app.health import storage as health_storage
from app.health.aggregator import aggregate_overall
from app.health.models import ComponentStatus, OverallHealth
from app.health.sanitize import logical_path_label, sanitize_config_summary, sanitize_path
from app.health.storage_metrics import collect_storage_metrics
from app.health.thresholds import (
    apply_consecutive_thresholds,
    copy_poll_stale_seconds,
    job_stale_seconds,
)
from app.storage.backup import backup_metrics_for_health
from app.storage.cleanup import cleanup_metrics_for_health
from app.storage.retention_prune import prune_metrics_for_health

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _update_state(
    settings: Settings,
    component: str,
    *,
    probe_ok: bool,
    detail: str,
    metrics: dict | None = None,
    hard_down: bool = False,
    forced_status: str | None = None,
) -> ComponentStatus:
    prior = health_storage.get_component_state(settings, component)
    failures = int(prior["consecutive_failures"]) if prior else 0
    successes = int(prior["consecutive_successes"]) if prior else 0
    if forced_status:
        status = forced_status  # type: ignore[assignment]
        if forced_status == "DISABLED":
            failures, successes = 0, 0
        elif probe_ok:
            failures, successes = 0, successes + 1
        else:
            failures, successes = failures + 1, 0
    else:
        status, failures, successes = apply_consecutive_thresholds(
            settings,
            probe_ok=probe_ok,
            consecutive_failures=failures,
            consecutive_successes=successes,
            hard_down=hard_down,
        )
    health_storage.upsert_component_state(
        settings,
        component=component,
        status=status,
        consecutive_failures=failures,
        consecutive_successes=successes,
        detail=detail,
        metrics=metrics,
    )
    health_storage.record_health_check(
        settings,
        component=component,
        status=status,
        detail=detail,
        metrics=metrics,
    )
    return ComponentStatus(
        component=component,
        status=status,  # type: ignore[arg-type]
        detail=detail,
        checked_at=_now_iso(),
        metrics=metrics or {},
    )


def probe_telegram(settings: Settings, bot_data: dict | None = None) -> ComponentStatus:
    bot_data = bot_data or {}
    last_update = bot_data.get("health_last_update_at")
    conflict = bool(bot_data.get("health_getupdates_conflict"))
    duplicate = bool(bot_data.get("health_duplicate_process"))
    startup_timeout = bool(bot_data.get("health_startup_timeout"))
    metrics = {
        "last_update_at": last_update,
        "getupdates_conflict": conflict,
        "duplicate_process": duplicate,
        "startup_timeout": startup_timeout,
        "polling": bool(bot_data.get("health_polling_active", True)),
    }
    if conflict or duplicate:
        return _update_state(
            settings,
            "telegram",
            probe_ok=False,
            hard_down=True,
            detail="Telegram polling conflict or duplicate process detected",
            metrics=metrics,
        )
    if startup_timeout:
        return _update_state(
            settings,
            "telegram",
            probe_ok=False,
            detail="Telegram startup timeout",
            metrics=metrics,
        )
    ts = _parse_iso(last_update) if isinstance(last_update, str) else None
    if ts is not None and (time.time() - ts) > 900:
        return _update_state(
            settings,
            "telegram",
            probe_ok=False,
            detail="No successful Telegram update recently",
            metrics=metrics,
        )
    return _update_state(
        settings,
        "telegram",
        probe_ok=True,
        detail="Telegram polling healthy",
        metrics=metrics,
    )


def probe_scheduler(settings: Settings) -> ComponentStatus:
    jobs = {
        "copy_intel_poll": settings.copy_poll_interval_seconds
        if settings.copy_intel_enabled
        else None,
        "health_monitor_tick": settings.health_check_interval_seconds
        if settings.health_monitor_enabled
        else None,
    }
    issues = []
    metrics: dict = {"jobs": {}}
    now = time.time()
    for name, interval in jobs.items():
        if interval is None:
            metrics["jobs"][name] = "disabled"
            continue
        row = health_storage.latest_job_execution(settings, name)
        metrics["jobs"][name] = {
            "last_status": row["status"] if row else None,
            "last_started": row["started_at"] if row else None,
            "last_finished": row["finished_at"] if row else None,
        }
        if row is None:
            # Not yet run — degraded only after stale window from process start is unknown
            continue
        started = _parse_iso(row["started_at"])
        if started is not None and (now - started) > job_stale_seconds(settings, interval):
            if row["finished_at"] is None or row["status"] == "error":
                issues.append(f"{name} stale/failed")
            else:
                finished = _parse_iso(row["finished_at"])
                if finished is not None and (now - finished) > job_stale_seconds(
                    settings, interval
                ):
                    issues.append(f"{name} overdue")
    if issues:
        return _update_state(
            settings,
            "scheduler",
            probe_ok=False,
            detail="; ".join(issues),
            metrics=metrics,
        )
    return _update_state(
        settings,
        "scheduler",
        probe_ok=True,
        detail="Scheduler jobs within expected windows",
        metrics=metrics,
    )


def probe_weather(settings: Settings) -> ComponentStatus:
    rows = health_storage.list_provider_metrics(settings)
    providers = {
        "nws",
        "met_norway",
        "ecmwf_ifs",
        "dwd_icon",
        "wunderground",
        "metar",
    }
    valid = 0
    errors = []
    metrics: dict = {"providers": {}}
    for row in rows:
        name = row["provider"]
        if name not in providers:
            continue
        ok_recent = bool(row["last_success_at"])
        fail_recent = bool(row["last_failure_at"])
        metrics["providers"][name] = {
            "success_count": row["success_count"],
            "failure_count": row["failure_count"],
            "last_status_code": row["last_status_code"],
        }
        if row["last_status_code"] in {401, 429} or (
            row["last_status_code"] and int(row["last_status_code"]) >= 500
        ):
            errors.append(f"{name}:{row['last_status_code']}")
        if ok_recent and not (
            fail_recent
            and _parse_iso(row["last_failure_at"])  # type: ignore[arg-type]
            and _parse_iso(row["last_success_at"])
            and _parse_iso(row["last_failure_at"]) > _parse_iso(row["last_success_at"])  # type: ignore[operator]
        ):
            valid += 1
    metrics["valid_provider_count"] = valid
    metrics["min_required"] = settings.forecast_min_providers
    if errors and valid < settings.forecast_min_providers:
        return _update_state(
            settings,
            "weather",
            probe_ok=False,
            detail=f"Provider errors; valid={valid}: {', '.join(errors[:5])}",
            metrics=metrics,
        )
    if rows and valid < settings.forecast_min_providers:
        return _update_state(
            settings,
            "weather",
            probe_ok=False,
            detail=f"Valid providers {valid} < min {settings.forecast_min_providers}",
            metrics=metrics,
        )
    # No metrics yet — not a failure (DISABLED-like healthy default)
    return _update_state(
        settings,
        "weather",
        probe_ok=True,
        detail="Weather providers within thresholds"
        if rows
        else "No provider metrics yet",
        metrics=metrics,
    )


def probe_copy_readonly(settings: Settings) -> ComponentStatus:
    if not settings.copy_intel_enabled:
        return _update_state(
            settings,
            "copy_readonly",
            probe_ok=True,
            forced_status="DISABLED",
            detail="Copy Intelligence disabled",
            metrics={"enabled": False},
        )
    if not settings.copy_intel_read_only:
        return _update_state(
            settings,
            "copy_readonly",
            probe_ok=False,
            hard_down=True,
            detail="READ-ONLY SAFETY VIOLATION: copy_intel_read_only is false",
            metrics={"read_only": False},
        )
    return _update_state(
        settings,
        "copy_readonly",
        probe_ok=True,
        detail="Copy Intelligence read-only confirmed",
        metrics={"read_only": True},
    )


def probe_crypto_readonly(settings: Settings) -> ComponentStatus:
    if not settings.crypto_intel_enabled:
        return _update_state(
            settings,
            "crypto_readonly",
            probe_ok=True,
            forced_status="DISABLED",
            detail="Crypto Intelligence disabled",
            metrics={"enabled": False},
        )
    if not settings.crypto_intel_read_only:
        return _update_state(
            settings,
            "crypto_readonly",
            probe_ok=False,
            hard_down=True,
            detail="READ-ONLY SAFETY VIOLATION: crypto_intel_read_only is false",
            metrics={"read_only": False},
        )
    return _update_state(
        settings,
        "crypto_readonly",
        probe_ok=True,
        detail="Crypto Intelligence read-only confirmed",
        metrics={"read_only": True},
    )


def probe_crypto_intelligence(settings: Settings) -> ComponentStatus:
    if not settings.crypto_intel_enabled:
        return _update_state(
            settings,
            "crypto_intel",
            probe_ok=True,
            forced_status="DISABLED",
            detail="Crypto Intelligence disabled",
            metrics={"enabled": False},
        )
    metrics: dict = {
        "read_only": settings.crypto_intel_read_only,
        "symbols": list(settings.crypto_symbols),
    }
    return _update_state(
        settings,
        "crypto_intel",
        probe_ok=True,
        detail="Crypto Intelligence enabled (public market data)",
        metrics=metrics,
    )


def probe_copy_intelligence(settings: Settings) -> ComponentStatus:
    if not settings.copy_intel_enabled:
        return _update_state(
            settings,
            "copy_intel",
            probe_ok=True,
            forced_status="DISABLED",
            detail="Copy Intelligence disabled",
            metrics={"enabled": False},
        )
    metrics: dict = {
        "read_only": settings.copy_intel_read_only,
        "watchlist": len(copy_storage.list_enabled_wallets(settings)),
    }

    last_poll = copy_storage.get_poll_state(settings, "last_poll_at")
    last_error = copy_storage.get_poll_state(settings, "last_error") or ""
    last_new = copy_storage.get_poll_state(settings, "last_new_event_at")
    stale_rejects = copy_storage.get_poll_state(settings, "last_stale_rejects") or "0"
    metrics.update(
        {
            "last_poll_at": last_poll,
            "last_new_event_at": last_new,
            "last_error": last_error[:200] if last_error else "",
            "stale_rejects": stale_rejects,
        }
    )
    from app.copy_intel.freshness import baseline_is_complete

    incomplete = 0
    failed_baselines = 0
    for wallet in copy_storage.list_enabled_wallets(settings):
        if not baseline_is_complete(settings, wallet):
            incomplete += 1
        failed_baselines += copy_storage.count_baseline_failures(settings, wallet, limit=5)
    metrics["baselines_incomplete"] = incomplete
    metrics["recent_baseline_failures"] = failed_baselines

    poll_ts = _parse_iso(last_poll)
    if poll_ts is not None and (time.time() - poll_ts) > copy_poll_stale_seconds(settings):
        return _update_state(
            settings,
            "copy_intel",
            probe_ok=False,
            detail="Copy poll stale",
            metrics=metrics,
        )
    if last_error:
        return _update_state(
            settings,
            "copy_intel",
            probe_ok=False,
            detail=f"Copy poll error: {last_error[:160]}",
            metrics=metrics,
        )
    if failed_baselines >= settings.health_failure_threshold:
        return _update_state(
            settings,
            "copy_intel",
            probe_ok=False,
            detail="Baseline repeatedly failing",
            metrics=metrics,
        )
    return _update_state(
        settings,
        "copy_intel",
        probe_ok=True,
        detail="Copy Intelligence healthy",
        metrics=metrics,
    )


def probe_sqlite(settings: Settings) -> ComponentStatus:
    try:
        path = Path(settings.database_path)
        with get_connection(settings) as connection:
            connection.execute("SELECT 1").fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        required = {
            "users",
            "copy_watchlist",
            "wallet_monitor_state",
            "health_checks",
            "component_states",
        }
        missing = sorted(required - tables)
        metrics = {
            "database": logical_path_label("live_database"),
            "database_basename": sanitize_path(path),
            "table_count": len(tables),
            "missing": missing,
        }
        if missing:
            return _update_state(
                settings,
                "sqlite",
                probe_ok=False,
                hard_down=True,
                detail=f"Missing tables: {', '.join(missing)}",
                metrics=metrics,
            )
        return _update_state(
            settings,
            "sqlite",
            probe_ok=True,
            detail="SQLite accessible; schema OK",
            metrics=metrics,
        )
    except sqlite3.Error as exc:
        return _update_state(
            settings,
            "sqlite",
            probe_ok=False,
            hard_down=True,
            detail=f"SQLite error: {exc}",
            metrics={},
        )


def probe_infrastructure(settings: Settings) -> ComponentStatus:
    prior = health_storage.get_component_state(settings, "infrastructure")
    prior_metrics = None
    if prior is not None:
        raw = prior["metrics_json"] if "metrics_json" in prior.keys() else None
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    prior_metrics = parsed
            except json.JSONDecodeError:
                prior_metrics = None

    storage = collect_storage_metrics(settings, prior_metrics=prior_metrics)
    cleanup_metrics = cleanup_metrics_for_health(settings)
    backup_metrics = backup_metrics_for_health(settings)
    prune_metrics = prune_metrics_for_health(settings)
    cfg = sanitize_config_summary(settings)
    metrics = {
        **storage.as_sanitized_dict(),
        **cleanup_metrics,
        **backup_metrics,
        **prune_metrics,
        "config": cfg,
        "cache_note": "scan_snapshots bounded per user/region",
        "retention_preview": "admin dry-run available via System Health",
    }

    if storage.level == "CRITICAL":
        return _update_state(
            settings,
            "infrastructure",
            probe_ok=False,
            hard_down=True,
            detail=storage.detail,
            metrics=metrics,
        )
    if storage.level == "DEGRADED":
        return _update_state(
            settings,
            "infrastructure",
            probe_ok=False,
            forced_status="DEGRADED",
            detail=storage.detail,
            metrics=metrics,
        )
    return _update_state(
        settings,
        "infrastructure",
        probe_ok=True,
        detail=storage.detail,
        metrics=metrics,
    )


def probe_opportunity_components(
    settings: Settings, bot_data: dict | None = None
) -> list[ComponentStatus]:
    """Sanitized opportunity scanner components (disabled-by-default)."""
    from app.opportunity_intel.health import (
        component_status_for_opportunity,
        evaluate_component_states,
    )

    snapshots = evaluate_component_states(settings, bot_data=bot_data)
    components: list[ComponentStatus] = []
    for name, snap in snapshots.items():
        forced = component_status_for_opportunity(snap.state)
        components.append(
            _update_state(
                settings,
                name,
                probe_ok=forced in {"HEALTHY", "DISABLED"},
                detail=snap.detail,
                metrics=snap.metrics,
                forced_status=forced,
                hard_down=forced == "DOWN",
            )
        )
    return components


def run_health_check(
    settings: Settings,
    *,
    bot_data: dict | None = None,
) -> OverallHealth:
    if not settings.health_monitor_enabled:
        disabled = ComponentStatus(
            component="health_monitor",
            status="DISABLED",
            detail="Health monitor disabled",
            checked_at=_now_iso(),
        )
        return aggregate_overall([disabled])

    components = [
        probe_sqlite(settings),
        probe_telegram(settings, bot_data=bot_data),
        probe_scheduler(settings),
        probe_weather(settings),
        probe_copy_intelligence(settings),
        probe_copy_readonly(settings),
        probe_crypto_intelligence(settings),
        probe_crypto_readonly(settings),
        probe_infrastructure(settings),
        *probe_opportunity_components(settings, bot_data=bot_data),
    ]
    overall = aggregate_overall(components)
    try:
        health_storage.prune_health_history(settings)
    except Exception:
        logger.debug("Health history prune skipped", exc_info=True)
    return overall
