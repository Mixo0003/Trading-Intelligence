"""Storage maintenance helpers (metrics, cleanup, retention audit/prune, backup)."""

from app.storage.backup import (
    backup_metrics_for_health,
    backup_status,
    create_online_backup,
    get_last_backup_report,
    inventory_expired_backups,
    revalidate_verified_backup_for_pruning,
)
from app.storage.cleanup import (
    cleanup_metrics_for_health,
    get_last_cleanup_report,
    inventory_cleanup_candidates,
    run_storage_cleanup,
)
from app.storage.log_rotation import ensure_rotating_file_logging
from app.storage.retention_audit import retention_metrics_for_health, run_retention_audit
from app.storage.retention_prune import (
    create_prune_confirmation,
    execute_confirmed_prune,
    get_last_prune_report,
    prune_metrics_for_health,
    run_retention_prune,
)

__all__ = [
    "backup_metrics_for_health",
    "backup_status",
    "cleanup_metrics_for_health",
    "create_online_backup",
    "create_prune_confirmation",
    "ensure_rotating_file_logging",
    "execute_confirmed_prune",
    "get_last_backup_report",
    "get_last_cleanup_report",
    "get_last_prune_report",
    "inventory_cleanup_candidates",
    "inventory_expired_backups",
    "prune_metrics_for_health",
    "retention_metrics_for_health",
    "revalidate_verified_backup_for_pruning",
    "run_retention_audit",
    "run_retention_prune",
    "run_storage_cleanup",
]
