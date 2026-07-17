"""Format sanitized health screens for Telegram."""

from __future__ import annotations

from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.models import OverallHealth
from app.health.sanitize import sanitize_config_summary, sanitize_mapping, sanitize_path
from app.health.self_test import run_self_test


def format_health_summary(overall: OverallHealth) -> str:
    lines = [
        "📊 Health Summary",
        f"Overall: {overall.status}",
        overall.summary,
        f"Checked: {overall.checked_at}",
        "",
        "Components:",
    ]
    for comp in overall.components:
        lines.append(f"• {comp.component}: {comp.status} — {comp.detail}")
    return "\n".join(lines)


def format_weather_health(settings: Settings) -> str:
    rows = health_storage.list_provider_metrics(settings)
    lines = ["🌦 Weather Providers"]
    if not rows:
        lines.append("No provider metrics recorded yet.")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"• {row['provider']}: ok={row['success_count']} fail={row['failure_count']} "
            f"code={row['last_status_code'] or '-'}"
        )
    return "\n".join(lines)


def format_copy_health(settings: Settings, overall: OverallHealth) -> str:
    copy = next((c for c in overall.components if c.component == "copy_intel"), None)
    ro = next((c for c in overall.components if c.component == "copy_readonly"), None)
    lines = ["👥 Copy Intelligence"]
    if copy:
        lines.append(f"Status: {copy.status}")
        lines.append(copy.detail)
        metrics = sanitize_mapping(copy.metrics)
        for key in (
            "last_poll_at",
            "last_new_event_at",
            "watchlist",
            "baselines_incomplete",
            "stale_rejects",
            "read_only",
        ):
            if key in metrics:
                lines.append(f"{key}: {metrics[key]}")
    if ro:
        lines.append(f"Read-only: {ro.status} — {ro.detail}")
    lines.append("")
    lines.append("Use Rebuild Copy Baselines to re-seed active subscriptions.")
    lines.append("No wallet addresses or secrets are shown.")
    return "\n".join(lines)


def format_rebuild_baselines_confirm(settings: Settings) -> str:
    from app.database import copy_storage

    active = len(copy_storage.list_enabled_wallets(settings))
    return "\n".join(
        [
            "🔄 Rebuild Copy Baselines",
            "",
            f"Active subscriptions: {active}",
            "This re-seeds baselines for enabled wallets only.",
            "Zero alerts during rebuild.",
            "Watchlist, paper, and analytics are preserved.",
            "",
            "Confirm to continue.",
        ]
    )


def format_rebuild_baselines_result(result: dict[str, int]) -> str:
    return "\n".join(
        [
            "🔄 Rebuild Copy Baselines — Done",
            f"Total: {result.get('total', 0)}",
            f"Succeeded: {result.get('succeeded', 0)}",
            f"Failed: {result.get('failed', 0)}",
            "",
            "No wallet addresses or secrets shown.",
            "Alerts stay off until each baseline is COMPLETE.",
        ]
    )


def format_jobs_health(settings: Settings) -> str:
    rows = health_storage.list_recent_job_executions(settings, limit=12)
    lines = ["⏱ Jobs & Scheduler"]
    if not rows:
        lines.append("No job executions recorded yet.")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"• {row['job_name']}: {row['status']} "
            f"({row['duration_ms'] or '-'} ms) {row['started_at']}"
        )
    return "\n".join(lines)


def format_db_health(settings: Settings, overall: OverallHealth) -> str:
    sqlite_c = next((c for c in overall.components if c.component == "sqlite"), None)
    infra = next((c for c in overall.components if c.component == "infrastructure"), None)
    lines = ["🗄 Database & Cache"]
    if sqlite_c:
        lines.append(f"SQLite: {sqlite_c.status} — {sqlite_c.detail}")
        metrics = sanitize_mapping(sqlite_c.metrics)
        if "database" in metrics:
            lines.append(f"DB file: {metrics['database']}")
        if "table_count" in metrics:
            lines.append(f"Tables: {metrics['table_count']}")
    if infra:
        lines.append(f"Infrastructure: {infra.status} — {infra.detail}")
        disk = sanitize_mapping(infra.metrics).get("disk_free_mb")
        if disk is not None:
            lines.append(f"Disk free MB: {disk}")
    lines.append(f"Configured DB: {sanitize_path(settings.database_path)}")
    return "\n".join(lines)


def format_incidents(settings: Settings) -> str:
    rows = health_storage.list_recent_incidents(settings, limit=15)
    lines = ["🚨 Recent Incidents"]
    if not rows:
        lines.append("No incidents recorded.")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"• [{row['state']}] {row['component']} {row['severity']}: {row['title']}"
        )
    return "\n".join(lines)


def format_self_test(settings: Settings, *, bot_data: dict | None = None) -> str:
    result = run_self_test(settings, bot_data=bot_data)
    lines = [
        "🧪 Self-Test Results",
        f"Overall: {result['overall']}",
        result["summary"],
        "",
    ]
    for comp in result["components"]:
        lines.append(f"• {comp['component']}: {comp['status']}")
    return "\n".join(lines)


def format_health_settings(settings: Settings) -> str:
    cfg = sanitize_config_summary(settings)
    lines = [
        "⚙️ Health Settings",
        f"Monitor enabled: {cfg['health_monitor_enabled']}",
        f"Check interval: {cfg['health_check_interval_seconds']}s",
        f"Alerts enabled: {cfg['health_alerts_enabled']}",
        f"Failure threshold: {settings.health_failure_threshold}",
        f"Recovery threshold: {settings.health_recovery_threshold}",
        f"Job stale multiplier: {settings.health_job_stale_multiplier}",
        f"AI explainer: {settings.health_ai_explainer_enabled} (unused)",
        f"Admins configured: {cfg['admin_count']}",
        "",
        "Core status never depends on AI.",
        "Secrets are never shown.",
    ]
    return "\n".join(lines)
