"""Sanitized maintenance history for admin Storage UX (V2.1.1 Phase 5)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.config.constants import (
    STORAGE_BACKUP_LOCK_JOB,
    STORAGE_CLEANUP_LOCK_JOB,
    STORAGE_RETENTION_PRUNE_LOCK_JOB,
)
from app.config.settings import Settings
from app.health import storage as health_storage

_MAINTENANCE_JOBS = frozenset(
    {
        STORAGE_BACKUP_LOCK_JOB,
        STORAGE_CLEANUP_LOCK_JOB,
        STORAGE_RETENTION_PRUNE_LOCK_JOB,
    }
)

_JOB_LABELS = {
    STORAGE_BACKUP_LOCK_JOB: "backup",
    STORAGE_CLEANUP_LOCK_JOB: "file_cleanup",
    STORAGE_RETENTION_PRUNE_LOCK_JOB: "retention_prune",
}


@dataclass(frozen=True)
class MaintenanceHistoryItem:
    operation: str
    outcome: str
    started_at: str
    finished_at: str | None
    backup_status: str | None
    candidate_count: int | None
    deleted_count: int | None
    skipped_count: int | None
    failure_code: str | None
    rollback_status: str | None
    dry_run: bool | None


def _safe_json(raw: str | None) -> dict:
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _map_outcome(job_status: str, payload: dict) -> str:
    status = str(payload.get("status") or job_status or "").upper()
    if status in {"DRY_RUN", "DISABLED", "INVENTORY"} or payload.get("dry_run") is True:
        if status == "DISABLED":
            return "REFUSED"
        return "DRY_RUN"
    if status in {"VERIFIED", "SUCCEEDED", "COMPLETED", "OK"}:
        return "SUCCEEDED"
    if status in {"REFUSED", "UNAVAILABLE", "BUSY", "LOCK_BUSY"}:
        return "REFUSED"
    if status in {"FAILED", "ERROR", "INCOMPLETE"} or job_status == "error":
        return "FAILED"
    if status:
        return status
    return "FAILED" if job_status == "error" else "SUCCEEDED"


def list_maintenance_history(
    settings: Settings,
    *,
    limit: int = 20,
    page: int = 0,
    page_size: int = 8,
) -> tuple[list[MaintenanceHistoryItem], int]:
    """Return sanitized page of recent maintenance operations and total count."""
    rows = health_storage.list_recent_job_executions(settings, limit=max(limit, 1))
    items: list[MaintenanceHistoryItem] = []
    for row in rows:
        job_name = str(row["job_name"] or "")
        if job_name not in _MAINTENANCE_JOBS:
            continue
        payload = _safe_json(row["error"] if "error" in row.keys() else None)
        deleted = payload.get("deleted_total", payload.get("deleted"))
        candidates = payload.get("candidate_total", payload.get("candidates"))
        skipped = payload.get("skipped_protected")
        if skipped is None and isinstance(payload.get("skipped_protected_total"), int):
            skipped = payload.get("skipped_protected_total")
        items.append(
            MaintenanceHistoryItem(
                operation=_JOB_LABELS.get(job_name, "maintenance"),
                outcome=_map_outcome(str(row["status"] or ""), payload),
                started_at=str(row["started_at"] or ""),
                finished_at=(str(row["finished_at"]) if row["finished_at"] else None),
                backup_status=(
                    str(payload.get("backup_label") or payload.get("integrity") or "")
                    or None
                ),
                candidate_count=int(candidates) if candidates is not None else None,
                deleted_count=int(deleted) if deleted is not None else None,
                skipped_count=int(skipped) if skipped is not None else None,
                failure_code=(
                    str(payload.get("failure_code"))
                    if payload.get("failure_code")
                    else None
                ),
                rollback_status=(
                    "rolled_back"
                    if payload.get("rolled_back") is True
                    else ("none" if "rolled_back" in payload else None)
                ),
                dry_run=payload.get("dry_run") if "dry_run" in payload else None,
            )
        )

    total = len(items)
    start = max(0, page) * page_size
    end = start + page_size
    return items[start:end], total
