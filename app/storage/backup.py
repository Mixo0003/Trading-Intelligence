"""Online SQLite backup and integrity gate (V2.1.1 Phase 4A)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import Settings
from app.health.sanitize import logical_path_label, sanitize_path
from app.storage.backup_paths import (
    backup_path_is_safe,
    reject_reason_for_backup_dir,
    resolve_backup_dir,
)
from app.storage.lock import acquire_backup_lock, release_backup_lock

logger = logging.getLogger(__name__)

_REQUIRED_TABLES = frozenset(
    {
        "users",
        "copy_watchlist",
        "wallet_monitor_state",
        "health_checks",
        "component_states",
    }
)
_PROTECTED_COUNT_TABLES = (
    "users",
    "copy_watchlist",
    "wallet_monitor_state",
    "user_filters",
    "copy_user_settings",
)

_LAST_BACKUP_REPORT = None


@dataclass
class BackupReport:
    status: str  # VERIFIED | FAILED | INCOMPLETE | UNAVAILABLE | BUSY | REFUSED
    configured: bool
    dry_run_retention: bool = True
    backup_label: str | None = None
    backup_size_bytes: int | None = None
    integrity_result: str | None = None
    created_at: str | None = None
    age_seconds: float | None = None
    detail: str = ""
    expired_candidate_count: int = 0
    future_retention_gate: str = "blocked_until_verified_backup"
    source_table_count: int | None = None
    backup_table_count: int | None = None
    protected_counts: dict[str, int] = field(default_factory=dict)
    mutation_proof: dict = field(default_factory=dict)

    def as_sanitized_dict(self) -> dict:
        return {
            "backup_configured": self.configured,
            "backup_status": self.status,
            "backup_label": self.backup_label or "(none)",
            "backup_size_bytes": self.backup_size_bytes,
            "backup_integrity": self.integrity_result,
            "backup_created_at": self.created_at,
            "backup_age_seconds": self.age_seconds,
            "backup_detail": self.detail,
            "backup_expired_candidates": self.expired_candidate_count,
            "backup_retention_gate": self.future_retention_gate,
            "backup_path_label": logical_path_label("backup"),
            "backup_dry_run_retention": self.dry_run_retention,
        }


def get_last_backup_report() -> BackupReport | None:
    return _LAST_BACKUP_REPORT


def _set_last(report: BackupReport) -> BackupReport:
    global _LAST_BACKUP_REPORT
    _LAST_BACKUP_REPORT = report
    return report


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _disk_free(path: Path) -> int | None:
    try:
        probe = path if path.exists() else path.parent
        if not probe.exists():
            probe = Path(".")
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return None


def _list_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _count_table(connection: sqlite3.Connection, table: str) -> int | None:
    try:
        row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return None


def _meta_path(backup_db: Path) -> Path:
    return Path(str(backup_db) + ".meta.json")


def _write_meta(backup_db: Path, payload: dict) -> None:
    meta = _meta_path(backup_db)
    tmp = Path(str(meta) + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, meta)


def _read_meta(backup_db: Path) -> dict | None:
    meta = _meta_path(backup_db)
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _verify_backup_file(backup_db: Path) -> tuple[str, str, dict]:
    """
    Returns (status, integrity_result, details).
    status: VERIFIED | FAILED
    """
    details: dict = {}
    try:
        uri = backup_db.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return "FAILED", f"open_error:{type(exc).__name__}", details
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        integrity_text = str(integrity[0]) if integrity else "missing"
        details["integrity_check"] = integrity_text
        quick = connection.execute("PRAGMA quick_check").fetchone()
        details["quick_check"] = str(quick[0]) if quick else "missing"
        tables = _list_tables(connection)
        details["table_count"] = len(tables)
        missing = sorted(_REQUIRED_TABLES - tables)
        details["missing_required"] = missing
        protected = {}
        for name in _PROTECTED_COUNT_TABLES:
            if name in tables:
                protected[name] = _count_table(connection, name)
        details["protected_counts"] = protected
        if integrity_text.lower() != "ok":
            return "FAILED", integrity_text, details
        if details["quick_check"].lower() not in {"ok", "missing"}:
            # quick_check may return ok; if present and not ok, fail
            if details["quick_check"].lower() != "ok":
                return "FAILED", f"quick_check:{details['quick_check']}", details
        if missing:
            return "FAILED", f"missing_tables:{','.join(missing)}", details
        return "VERIFIED", integrity_text, details
    except sqlite3.Error as exc:
        return "FAILED", f"verify_error:{type(exc).__name__}", details
    finally:
        connection.close()


def inventory_expired_backups(settings: Settings) -> tuple[list[str], int]:
    """
    Dry-run retention preview. Never deletes.
    Returns (candidate labels, count). Protects newest and only verified backup.
    """
    root = resolve_backup_dir(
        settings.storage_backup_dir, database_path=settings.database_path
    )
    if root is None or not root.exists():
        return [], 0

    verified: list[tuple[float, Path, dict]] = []
    for path in sorted(root.glob("*.db")):
        if not backup_path_is_safe(
            path, backup_root=root, database_path=settings.database_path
        ):
            continue
        meta = _read_meta(path) or {}
        if meta.get("status") != "VERIFIED":
            continue
        created = meta.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = path.stat().st_mtime
        verified.append((ts, path, meta))

    if not verified:
        return [], 0

    verified.sort(key=lambda item: item[0], reverse=True)
    newest = verified[0][1]
    max_count = max(1, settings.storage_backup_max_count)
    max_age = max(1, settings.storage_backup_max_age_days) * 86400
    now = _now().timestamp()

    # Keep newest max_count verified; older beyond age or beyond count are candidates.
    keep: set[Path] = {item[1] for item in verified[:max_count]}
    keep.add(newest)
    if len(verified) == 1:
        return [], 0

    candidates: list[str] = []
    for ts, path, _meta in verified:
        if path == newest:
            continue
        if path in keep and (now - ts) <= max_age:
            continue
        # Candidate if over count rank or too old
        rank_over = path not in {item[1] for item in verified[:max_count]}
        age_over = (now - ts) > max_age
        if rank_over or age_over:
            # Still never delete if it would leave zero verified — already protected newest
            candidates.append(sanitize_path(path.name))
    return candidates, len(candidates)


def backup_status(settings: Settings) -> BackupReport:
    """Read-only status for Health UI."""
    reason = reject_reason_for_backup_dir(
        settings.storage_backup_dir, database_path=settings.database_path
    )
    configured = reason is None
    expired_labels, expired_count = inventory_expired_backups(settings)
    last = get_last_backup_report()

    # Prefer scanning latest verified on disk.
    root = resolve_backup_dir(
        settings.storage_backup_dir, database_path=settings.database_path
    )
    latest: BackupReport | None = None
    if root and root.exists():
        verified_files: list[tuple[float, Path, dict]] = []
        for path in root.glob("*.db"):
            meta = _read_meta(path) or {}
            if meta.get("status") != "VERIFIED":
                continue
            created = meta.get("created_at") or ""
            try:
                ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = path.stat().st_mtime
            verified_files.append((ts, path, meta))
        if verified_files:
            verified_files.sort(key=lambda i: i[0], reverse=True)
            ts, path, meta = verified_files[0]
            age = max(0.0, _now().timestamp() - ts)
            latest = BackupReport(
                status="VERIFIED",
                configured=configured,
                backup_label=sanitize_path(path.name),
                backup_size_bytes=int(path.stat().st_size),
                integrity_result=str(meta.get("integrity_result") or "ok"),
                created_at=str(meta.get("created_at") or ""),
                age_seconds=round(age, 1),
                detail="Latest verified backup on disk",
                expired_candidate_count=expired_count,
                future_retention_gate="ready_for_future_prune_gate",
                protected_counts=dict(meta.get("protected_counts") or {}),
            )

    if latest is None and last is not None:
        latest = last
        latest.expired_candidate_count = expired_count
    if latest is None:
        status = "UNAVAILABLE" if not configured else "UNAVAILABLE"
        detail = "backup directory not configured" if reason == "empty" else (
            f"backup unavailable: {reason}" if reason else "no verified backup yet"
        )
        latest = BackupReport(
            status=status,
            configured=configured,
            detail=detail,
            expired_candidate_count=expired_count,
            future_retention_gate="blocked_until_verified_backup",
        )
    else:
        latest.configured = configured
        latest.expired_candidate_count = expired_count
        if latest.status == "VERIFIED":
            latest.future_retention_gate = "ready_for_future_prune_gate"
        else:
            latest.future_retention_gate = "blocked_until_verified_backup"
    # Attach dry-run candidate labels count only (not absolute paths)
    _ = expired_labels
    return latest


def create_online_backup(settings: Settings) -> BackupReport:
    """
    Create an online SQLite backup with integrity verification.
    Never uses ordinary filesystem copy of the live DB.
    Never deletes backups. Never restores over live DB.
    """
    reason = reject_reason_for_backup_dir(
        settings.storage_backup_dir, database_path=settings.database_path
    )
    if reason is not None:
        return _set_last(
            BackupReport(
                status="UNAVAILABLE",
                configured=False,
                detail=f"backup unavailable: {reason}",
                future_retention_gate="blocked_until_verified_backup",
            )
        )

    root = resolve_backup_dir(
        settings.storage_backup_dir, database_path=settings.database_path
    )
    if root is None:
        return _set_last(
            BackupReport(
                status="UNAVAILABLE",
                configured=False,
                detail="backup directory rejected",
                future_retention_gate="blocked_until_verified_backup",
            )
        )

    db_path = Path(settings.database_path)
    if str(db_path) == ":memory:" or not db_path.exists():
        return _set_last(
            BackupReport(
                status="FAILED",
                configured=True,
                detail="source database unavailable",
                future_retention_gate="blocked_until_verified_backup",
            )
        )

    free = _disk_free(root)
    if free is not None and free < settings.storage_backup_min_free_bytes:
        return _set_last(
            BackupReport(
                status="REFUSED",
                configured=True,
                detail="low disk — backup refused",
                future_retention_gate="blocked_until_verified_backup",
            )
        )

    owner = acquire_backup_lock(settings)
    if owner is None:
        return _set_last(
            BackupReport(
                status="BUSY",
                configured=True,
                detail="backup lock held",
                future_retention_gate="blocked_until_verified_backup",
            )
        )

    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    unique = uuid.uuid4().hex[:8]
    label = f"{settings.storage_backup_label}_{stamp}_{unique}.db"
    final_path = root / label
    partial_path = root / f"{label}.partial"
    source_size_before = None
    source_mtime_before = None
    source_protected_before: dict[str, int | None] = {}

    try:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return _set_last(
                BackupReport(
                    status="FAILED",
                    configured=True,
                    detail="cannot create backup directory",
                    future_retention_gate="blocked_until_verified_backup",
                )
            )

        if final_path.exists():
            return _set_last(
                BackupReport(
                    status="FAILED",
                    configured=True,
                    detail="backup filename collision — refusing overwrite",
                    future_retention_gate="blocked_until_verified_backup",
                )
            )

        if not backup_path_is_safe(
            partial_path, backup_root=root, database_path=settings.database_path
        ) or not backup_path_is_safe(
            final_path, backup_root=root, database_path=settings.database_path
        ):
            return _set_last(
                BackupReport(
                    status="FAILED",
                    configured=True,
                    detail="backup path failed safety checks",
                    future_retention_gate="blocked_until_verified_backup",
                )
            )

        source_size_before = db_path.stat().st_size
        source_mtime_before = db_path.stat().st_mtime

        # Online backup API via application database_path — never shutil.copy.
        source = sqlite3.connect(str(db_path), timeout=30.0)
        source.row_factory = sqlite3.Row
        try:
            source_tables = _list_tables(source)
            for name in _PROTECTED_COUNT_TABLES:
                if name in source_tables:
                    source_protected_before[name] = _count_table(source, name)
            dest = sqlite3.connect(str(partial_path))
            try:
                # pages>0 yields to the event loop between chunks on long DBs.
                source.backup(dest, pages=100)
            finally:
                dest.close()
        finally:
            source.close()

        status, integrity, details = _verify_backup_file(partial_path)
        created = _now_iso()
        backup_protected = dict(details.get("protected_counts") or {})
        # Soft metadata compare — count drift must not fail online backups.
        soft_mismatch = {
            name: {"source": source_protected_before.get(name), "backup": backup_protected.get(name)}
            for name in _PROTECTED_COUNT_TABLES
            if name in source_protected_before
            and name in backup_protected
            and source_protected_before.get(name) != backup_protected.get(name)
        }
        meta = {
            "status": status,
            "integrity_result": integrity,
            "created_at": created,
            "label": label,
            "protected_counts": backup_protected,
            "source_protected_counts": {
                k: v for k, v in source_protected_before.items() if v is not None
            },
            "soft_count_mismatch": soft_mismatch,
            "table_count": details.get("table_count"),
            "source_table_count": len(source_tables),
            "missing_required": details.get("missing_required") or [],
            "mechanism": "sqlite3.Connection.backup",
        }

        if status != "VERIFIED":
            try:
                if partial_path.exists():
                    partial_path.unlink()
                meta_partial = _meta_path(partial_path)
                if meta_partial.exists():
                    meta_partial.unlink()
            except OSError:
                pass
            return _set_last(
                BackupReport(
                    status="FAILED" if status == "FAILED" else "INCOMPLETE",
                    configured=True,
                    backup_label=sanitize_path(label),
                    integrity_result=integrity,
                    created_at=created,
                    detail="backup verification failed",
                    future_retention_gate="blocked_until_verified_backup",
                    protected_counts=backup_protected,
                    mutation_proof={
                        "source_size_before": source_size_before,
                        "source_size_after": db_path.stat().st_size,
                        "source_size_unchanged": db_path.stat().st_size == source_size_before,
                        "mechanism": "sqlite3.Connection.backup",
                        "no_shutil_copy": True,
                    },
                )
            )

        # Atomic promote — never overwrite an existing verified backup.
        os.replace(partial_path, final_path)
        _write_meta(final_path, meta)

        size = final_path.stat().st_size
        source_size_after = db_path.stat().st_size
        expired_labels, expired_count = inventory_expired_backups(settings)
        _ = expired_labels
        detail = "online backup verified"
        if soft_mismatch:
            detail = "online backup verified (soft protected-count drift noted)"
        return _set_last(
            BackupReport(
                status="VERIFIED",
                configured=True,
                backup_label=sanitize_path(label),
                backup_size_bytes=size,
                integrity_result=integrity,
                created_at=created,
                age_seconds=0.0,
                detail=detail,
                expired_candidate_count=expired_count,
                future_retention_gate="ready_for_future_prune_gate",
                source_table_count=len(source_tables),
                backup_table_count=details.get("table_count"),
                protected_counts=backup_protected,
                mutation_proof={
                    "source_size_before": source_size_before,
                    "source_size_after": source_size_after,
                    "source_mtime_before": source_mtime_before,
                    "source_size_unchanged": source_size_after == source_size_before,
                    "mechanism": "sqlite3.Connection.backup",
                    "no_shutil_copy": True,
                    "no_live_overwrite": True,
                    "soft_count_mismatch": soft_mismatch,
                },
            )
        )
    except Exception as exc:
        logger.debug("Online backup failed", exc_info=True)
        try:
            if partial_path.exists():
                partial_path.unlink()
            meta_partial = _meta_path(partial_path)
            if meta_partial.exists():
                meta_partial.unlink()
        except OSError:
            pass
        return _set_last(
            BackupReport(
                status="INCOMPLETE",
                configured=True,
                detail=f"backup interrupted: {type(exc).__name__}",
                future_retention_gate="blocked_until_verified_backup",
                mutation_proof={
                    "source_size_before": source_size_before,
                    "mechanism": "sqlite3.Connection.backup",
                    "no_shutil_copy": True,
                },
            )
        )
    finally:
        release_backup_lock(settings, owner)


def backup_metrics_for_health(settings: Settings) -> dict:
    """Sanitized backup status for infrastructure probe / System Health."""
    report = backup_status(settings)
    return report.as_sanitized_dict()
