"""Format sanitized health screens for Telegram."""

from __future__ import annotations

from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.diagnostics import format_component_ui_row, format_grouped_summary
from app.health.models import OverallHealth
from app.health.registry import STATUS_LABEL_UK, UI_GROUP_ORDER, label_uk_for
from app.health.sanitize import redact_string, sanitize_config_summary, sanitize_mapping
from app.health.self_test import run_self_test


def format_health_summary(overall: OverallHealth) -> str:
    return format_grouped_summary(overall, groups_order=UI_GROUP_ORDER)


def format_weather_health(settings: Settings) -> str:
    rows = health_storage.list_provider_metrics(settings)
    lines = ["🌦 Weather Intelligence"]
    if not rows:
        lines.append("Метрик провайдерів ще немає.")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"• {row['provider']}: ok={row['success_count']} fail={row['failure_count']} "
            f"code={row['last_status_code'] or '-'}"
        )
    lines.append("Застарілі прогнози не показуються як актуальні.")
    return "\n".join(lines)


def format_copy_health(settings: Settings, overall: OverallHealth) -> str:
    copy = next((c for c in overall.components if c.component == "copy_intel"), None)
    ro = next((c for c in overall.components if c.component == "copy_readonly"), None)
    lines = ["👥 Copy Intelligence"]
    if copy:
        lines.append(format_component_ui_row(copy))
    if ro:
        lines.append(format_component_ui_row(ro))
    lines.append("")
    lines.append("Rebuild Copy Baselines пересіває активні підписки.")
    lines.append("Адреси гаманців і секрети не показуються.")
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
    lines = ["🗄 Сховище / SQLite"]
    if sqlite_c:
        lines.append(format_component_ui_row(sqlite_c))
    if infra:
        lines.append(format_component_ui_row(infra))
        metrics = sanitize_mapping(infra.metrics)
        level = metrics.get("storage_level")
        if level is not None:
            lines.append(f"Storage level: {level}")
            lines.append(f"Рівень сховища: {level}")
        disk = metrics.get("disk_free_mb")
        if disk is not None:
            lines.append(f"Disk free MB: {disk}")
        if metrics.get("cleanup_candidate_count") is not None:
            lines.append(f"Cleanup candidates: {metrics['cleanup_candidate_count']}")
        if metrics.get("cleanup_reclaimable_bytes") is not None:
            lines.append(
                f"Cleanup reclaimable bytes: {metrics['cleanup_reclaimable_bytes']}"
            )
        if metrics.get("backup_status") is not None:
            lines.append(f"Backup status: {metrics.get('backup_status')}")
        if "cleanup_enabled" in metrics:
            lines.append(
                "Cleanup enabled: "
                f"{metrics.get('cleanup_enabled', False)}; "
                f"dry-run: {metrics.get('cleanup_dry_run', True)}"
            )
    lines.append("Configured DB: live_database")
    lines.append("Абсолютні шляхи та секрети не показуються.")
    return "\n".join(lines)


def format_backup_status(settings: Settings) -> str:
    """Admin-only backup status. Never exposes absolute paths."""
    from app.storage.backup import backup_status

    report = backup_status(settings)
    lines = [
        "💾 Online Backup Status",
        "Статус online backup — лише перегляд, без restore/prune.",
        f"Configured: {'yes' if report.configured else 'no'}",
        f"Налаштовано: {'так' if report.configured else 'ні'}",
        f"Status: {report.status}",
        f"Detail: {redact_string(str(report.detail or ''))}",
    ]
    if report.backup_label:
        lines.append(f"Мітка: {report.backup_label}")
    if report.backup_size_bytes is not None:
        lines.append(f"Розмір (байти): {report.backup_size_bytes}")
    if report.integrity_result:
        lines.append(f"Цілісність: {report.integrity_result}")
    if report.created_at:
        lines.append(f"Перевірено: {report.created_at}")
    if report.age_seconds is not None:
        lines.append(f"Вік (с): {report.age_seconds}")
    lines.append(f"Застарілі кандидати (dry-run): {report.expired_candidate_count}")
    lines.append(f"Retention gate: {report.future_retention_gate}")
    lines.append("Мітка шляху: backup")
    lines.append("Абсолютні шляхи та секрети не показуються.")
    return "\n".join(lines)


def format_backup_confirm(settings: Settings) -> str:
    configured = bool(getattr(settings, "storage_backup_dir", "") or "")
    return "\n".join(
        [
            "💾 Create Online Backup",
            "This creates a verified SQLite online backup only.",
            "No cleanup, restore, prune, or VACUUM.",
            f"Backup directory configured: {'yes' if configured else 'no'}",
            "Confirm to continue.",
        ]
    )


def format_backup_result(report) -> str:
    lines = [
        "💾 Backup Result",
        f"Status: {report.status}",
        f"Detail: {report.detail}",
    ]
    if report.backup_label:
        lines.append(f"Label: {report.backup_label}")
    if report.backup_size_bytes is not None:
        lines.append(f"Size bytes: {report.backup_size_bytes}")
    if report.integrity_result:
        lines.append(f"Integrity: {report.integrity_result}")
    if report.created_at:
        lines.append(f"Created: {report.created_at}")
    lines.append(f"Future retention gate: {report.future_retention_gate}")
    lines.append("No absolute paths shown.")
    return "\n".join(lines)


def format_retention_preview(settings: Settings, *, page: int = 0, page_size: int = 12) -> str:
    """Admin-only dry-run retention preview. Never deletes rows."""
    from app.storage.retention_audit import run_retention_audit

    report = run_retention_audit(settings)
    lines = [
        "📋 Retention Dry-Run Preview",
        "DRY RUN — no database rows were deleted.",
        f"Status: {report.status}",
        f"Checked: {report.checked_at}",
        "",
    ]
    if report.registry_errors:
        lines.append("Registry errors: " + "; ".join(report.registry_errors[:3]))
        lines.append("")

    proof = report.mutation_proof or {}
    if proof.get("row_counts_unchanged") is True:
        lines.append("Mutation proof: row counts unchanged")
    elif proof:
        lines.append("Mutation proof: see audit status")
    lines.append("")

    # Compact listing with pagination by policy order.
    start = max(0, page) * page_size
    end = start + page_size
    slice_policies = report.policies[start:end]
    if not report.policies:
        lines.append("No policies evaluated.")
    for item in slice_policies:
        cand = item.candidate_rows if item.candidate_rows is not None else "-"
        total = item.total_rows if item.total_rows is not None else "-"
        lines.append(
            f"• [{item.status}] {item.dataset}: total={total} candidates={cand}"
        )
        if item.status == "APPROVED" and item.retention_cutoff_iso:
            age = (
                f"{int(item.oldest_candidate_age_seconds)}s"
                if item.oldest_candidate_age_seconds is not None
                else "-"
            )
            lines.append(
                f"  cutoff_days_policy batch={item.future_batch_limit} oldest_candidate_age={age}"
            )
        lines.append(f"  {item.reason}")

    total_pages = max(1, (len(report.policies) + page_size - 1) // page_size)
    lines.append("")
    lines.append(f"Page {page + 1}/{total_pages}")
    lines.append("Scope is row counts only — not reclaimable disk bytes.")
    lines.append("No wallets, secrets, paths, or message contents shown.")
    return "\n".join(lines)


def format_incidents(settings: Settings) -> str:
    rows = health_storage.list_recent_incidents(settings, limit=15)
    lines = ["🚨 Останні інциденти"]
    if not rows:
        lines.append("Інцидентів немає.")
        return "\n".join(lines)
    for row in rows:
        label = label_uk_for(str(row["component"]))
        title = str(row["title"] or "")
        # Never echo raw component ids if they leaked into stored titles.
        for cid in (str(row["component"]),):
            title = title.replace(cid, label)
        lines.append(
            f"• [{row['state']}] {label} {row['severity']}: {title}"
        )
    return "\n".join(lines)


def format_self_test(settings: Settings, *, bot_data: dict | None = None) -> str:
    result = run_self_test(settings, bot_data=bot_data)
    lines = [
        "🧪 Self-Test",
        f"Загалом: {STATUS_LABEL_UK.get(result['overall'], result['overall'])}",
        result["summary"],
        "",
    ]
    for comp in result["components"]:
        lines.append(
            f"• {label_uk_for(comp['component'])}: "
            f"{STATUS_LABEL_UK.get(comp['status'], comp['status'])}"
        )
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


def _bytes_label(value: int | None) -> str:
    if value is None:
        return "-"
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    return f"{value / (1024 * 1024 * 1024):.2f} GiB"


def _age_label(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    sec = int(seconds)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def format_storage_menu(settings: Settings, overall: OverallHealth | None = None) -> str:
    """Compact admin Storage overview. Never exposes absolute paths."""
    from app.health.monitor import run_health_check
    from app.storage.backup import backup_status, revalidate_verified_backup_for_pruning
    from app.storage.cleanup import get_last_cleanup_report
    from app.storage.retention_prune import get_last_prune_report

    if overall is None:
        overall = run_health_check(settings)
    infra = next((c for c in overall.components if c.component == "infrastructure"), None)
    metrics = sanitize_mapping(infra.metrics if infra else {})
    backup = backup_status(settings)
    gate = revalidate_verified_backup_for_pruning(
        settings,
        max_age_seconds=settings.storage_retention_prune_max_backup_age_seconds,
    )
    last_cleanup = get_last_cleanup_report()
    last_prune = get_last_prune_report()

    lines = [
        "📦 Storage",
        f"Infrastructure: {infra.status if infra else '-'}",
        f"Storage level: {metrics.get('storage_level', '-')}",
        (
            f"Disk free: {_bytes_label(metrics.get('disk_free_bytes'))} "
            f"({metrics.get('disk_free_percent', '-')}%)"
        ),
        f"Live DB: {_bytes_label(metrics.get('database_size_bytes'))}",
        (
            "WAL/SHM/journal: "
            f"{_bytes_label(metrics.get('wal_size_bytes'))}/"
            f"{_bytes_label(metrics.get('shm_size_bytes'))}/"
            f"{_bytes_label(metrics.get('journal_size_bytes'))}"
        ),
        f"Journal mode: {metrics.get('journal_mode') or '-'}",
    ]
    row_counts = metrics.get("table_row_counts")
    if isinstance(row_counts, dict):
        for name in (
            "users",
            "processed_copy_events",
            "copy_watchlist",
            "health_checks",
            "wallet_event_watermarks",
        ):
            if name in row_counts:
                lines.append(f"{name}: {row_counts[name]}")
    lines.extend(
        [
            (
                "Backup config: "
                + (
                    "enabled"
                    if settings.storage_backup_enabled and backup.configured
                    else ("configured/disabled" if backup.configured else "unavailable")
                )
            ),
            f"Latest backup: {backup.status} age={_age_label(backup.age_seconds)}",
            f"Integrity: {backup.integrity_result or '-'}",
            f"Retention gate: {'READY' if gate.ok else 'BLOCKED'}",
            (
                "Last file cleanup: "
                + (last_cleanup.status if last_cleanup else "never")
            ),
            (
                "Last retention prune: "
                + (last_prune.status if last_prune else "never")
            ),
            "",
            "No absolute paths, secrets, wallets, or Telegram IDs shown.",
        ]
    )
    return "\n".join(lines)


def format_storage_metrics(settings: Settings, overall: OverallHealth | None = None) -> str:
    from app.health.monitor import run_health_check

    text = format_db_health(
        settings,
        overall if overall is not None else run_health_check(settings),
    )
    return "📈 Storage Metrics\n" + "\n".join(text.splitlines()[1:])


def format_storage_backup_confirm(settings: Settings) -> str:
    configured = bool(getattr(settings, "storage_backup_dir", "") or "")
    enabled = bool(settings.storage_backup_enabled)
    lines = [
        "🛡 Create Backup",
        "Confirmation required.",
        "Creates a verified SQLite online backup only.",
        "No cleanup, restore, prune, or VACUUM.",
        f"Configured: {'yes' if configured else 'no'}",
        f"Enabled: {'yes' if enabled else 'no — UNAVAILABLE'}",
        "Confirm binds admin + database identity + config and expires.",
    ]
    return "\n".join(lines)


def format_storage_cleanup_preview(settings: Settings) -> str:
    from app.storage.cleanup import inventory_cleanup_candidates

    _candidates, report = inventory_cleanup_candidates(settings)
    lines = [
        "🧹 File Cleanup Preview",
        "DRY RUN — no files deleted.",
        f"Enabled: {report.cleanup_enabled}",
        f"Dry-run default: {settings.storage_cleanup_dry_run}",
        f"Candidates: {report.candidate_count}",
        f"Bounded reclaimable: {_bytes_label(report.reclaimable_bytes)}",
        f"Skipped protected: {report.skipped_protected}",
        f"Roots: {', '.join(sorted(report.roots.values())) or '(none)'}",
        "",
        "Private paths are never shown.",
    ]
    if not report.cleanup_enabled:
        lines.append("Action UNAVAILABLE — STORAGE_CLEANUP_ENABLED=false.")
    return "\n".join(lines)


def format_storage_cleanup_confirm(settings: Settings, report) -> str:
    lines = [
        "🗑 Run Safe File Cleanup",
        "DESTRUCTIVE ACTION — second confirmation required.",
        f"Enabled: {settings.storage_cleanup_enabled}",
        f"Dry-run enforced: {settings.storage_cleanup_dry_run}",
        f"Candidates: {report.candidate_count}",
        f"Bounded bytes: {_bytes_label(report.reclaimable_bytes)}",
        f"Max files/run: {settings.storage_cleanup_max_files_per_run}",
        f"Max bytes/run: {_bytes_label(settings.storage_cleanup_max_bytes_per_run)}",
        "",
        "Confirmation binds candidate fingerprint + limits and expires.",
        "Changed candidates invalidate confirmation.",
    ]
    if not settings.storage_cleanup_enabled:
        lines.append("UNAVAILABLE — cleanup disabled by configuration.")
    elif settings.storage_cleanup_dry_run:
        lines.append("UNAVAILABLE — dry-run still enforced; destructive refuse.")
    return "\n".join(lines)


def format_storage_cleanup_result(report) -> str:
    return "\n".join(
        [
            "🗑 File Cleanup Result",
            f"Status: {report.status}",
            f"DRY RUN: {report.dry_run}",
            f"Deleted: {report.deleted_count} files / {_bytes_label(report.deleted_bytes)}",
            f"Skipped protected/locked/limits: "
            f"{report.skipped_protected}/{report.skipped_locked}/{report.skipped_limits}",
            f"Failed: {report.errors}",
            f"Detail: {report.detail or report.failure_code or '-'}",
            "No private paths shown.",
        ]
    )


def format_storage_prune_confirm(settings: Settings, report, gate) -> str:
    lines = [
        "✂ Run Retention Batch",
        "DESTRUCTIVE ACTION — second confirmation required.",
        "Logical row deletion only — does not reclaim DB file bytes.",
        f"Enabled: {settings.storage_retention_prune_enabled}",
        f"Dry-run default: {settings.storage_retention_prune_dry_run}",
        f"Backup gate: {'READY' if gate.ok else 'BLOCKED'}",
        f"Backup label: {gate.backup_label or '-'}",
        f"Backup age: {_age_label(gate.age_seconds)}",
        f"Candidate total: {sum(report.candidate_counts.values())}",
        "",
        "Policies:",
    ]
    for dataset, count in list(report.candidate_counts.items())[:12]:
        lines.append(f"• {dataset}: {count}")
    if len(report.candidate_counts) > 12:
        lines.append(f"• … +{len(report.candidate_counts) - 12} more")
    lines.append("")
    lines.append("Token binds admin, DB identity, backup, policies, cutoffs, counts.")
    if not gate.ok:
        lines.append(f"BLOCKED — {gate.failure_code or gate.detail}")
    if not settings.storage_retention_prune_enabled:
        lines.append("UNAVAILABLE — retention pruning disabled.")
    elif settings.storage_retention_prune_dry_run:
        lines.append("UNAVAILABLE — dry-run still enforced.")
    return "\n".join(lines)


def format_storage_prune_result(report) -> str:
    lines = [
        "✂ Retention Batch Result",
        f"Status: {report.status}",
        f"DRY RUN: {report.dry_run}",
        f"Deleted rows (logical): {sum(report.deleted_counts.values())}",
        f"Backup: {report.backup_label or '-'}",
        f"Failure: {report.failure_code or '-'}",
        f"Rollback: {'yes' if report.rolled_back else 'no'}",
        "",
        "Deleted by policy:",
    ]
    if report.deleted_counts:
        for dataset, count in report.deleted_counts.items():
            lines.append(f"• {dataset}: {count}")
    else:
        lines.append("• (none)")
    lines.append("")
    lines.append("Row deletion only — disk space is not immediately reclaimed.")
    lines.append("No paths, SQL, or row IDs shown.")
    return "\n".join(lines)


def format_maintenance_history(settings: Settings, *, page: int = 0) -> str:
    from app.storage.history import list_maintenance_history

    items, total = list_maintenance_history(settings, page=page, page_size=8)
    page_size = 8
    total_pages = max(1, (total + page_size - 1) // page_size)
    lines = [
        "📜 Maintenance History",
        f"Page {page + 1}/{total_pages} ({total} ops)",
        "",
    ]
    if not items:
        lines.append("No maintenance operations recorded.")
        return "\n".join(lines)
    for item in items:
        lines.append(
            f"• {item.operation} [{item.outcome}] "
            f"{item.started_at}"
        )
        bits = []
        if item.dry_run is True:
            bits.append("DRY_RUN")
        if item.candidate_count is not None:
            bits.append(f"cand={item.candidate_count}")
        if item.deleted_count is not None:
            bits.append(f"del={item.deleted_count}")
        if item.skipped_count is not None:
            bits.append(f"skip={item.skipped_count}")
        if item.backup_status:
            bits.append(f"backup={item.backup_status}")
        if item.failure_code:
            bits.append(f"fail={item.failure_code}")
        if item.rollback_status and item.rollback_status != "none":
            bits.append(f"rollback={item.rollback_status}")
        if bits:
            lines.append("  " + " ".join(bits))
    lines.append("")
    lines.append("No private paths, raw SQL, or secrets shown.")
    return "\n".join(lines)
