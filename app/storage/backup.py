"""Online SQLite backup and integrity gate (V2.1.1 Phase 4A)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config.constants import STORAGE_BACKUP_LOCK_JOB
from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.sanitize import logical_path_label, sanitize_path
from app.storage.backup_paths import (
    backup_path_is_safe,
    reject_reason_for_backup_dir,
    resolve_backup_dir,
)
from app.storage.lock import (
    acquire_backup_lock,
    fresh_exclusive_lock_job,
    release_backup_lock,
)

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
_CONFIRMATIONS: dict[str, "BackupConfirmation"] = {}


def compute_source_database_identity(database_path: str) -> str:
    """Stable non-path identity for live DB ↔ backup binding (hash only)."""
    try:
        resolved = str(Path(database_path).resolve())
    except OSError:
        resolved = str(database_path or "")
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]


def source_database_basename(database_path: str) -> str:
    return sanitize_path(database_path)


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


@dataclass(frozen=True)
class BackupConfirmation:
    token: str
    operator_id: str
    database_identity: str
    backup_dir_identity: str
    backup_enabled: bool
    backup_label_prefix: str
    expires_at: float
    preview_fingerprint: str


def get_last_backup_report() -> BackupReport | None:
    return _LAST_BACKUP_REPORT


def _set_last(report: BackupReport) -> BackupReport:
    global _LAST_BACKUP_REPORT
    _LAST_BACKUP_REPORT = report
    return report


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _backup_dir_identity(settings: Settings) -> str:
    raw = (settings.storage_backup_dir or "").strip()
    if not raw:
        return ""
    try:
        resolved = str(Path(raw).resolve())
    except OSError:
        resolved = raw
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]


def _record_backup_audit(settings: Settings, report: BackupReport) -> None:
    """Persist sanitized backup outcome in job_executions for maintenance history."""
    try:
        summary = json.dumps(
            {
                "status": report.status,
                "integrity": report.integrity_result,
                "label": report.backup_label,
                "size_bytes": report.backup_size_bytes,
                "gate": report.future_retention_gate,
                "detail": report.detail,
            },
            sort_keys=True,
        )
        if any(x in summary.lower() for x in ("token", "wallet", ":\\", "/users/", "/home/")):
            summary = '{"status":"sanitized"}'
        started = report.created_at or _now_iso()
        health_storage.insert_job_execution(
            settings,
            job_name=STORAGE_BACKUP_LOCK_JOB,
            status="ok" if report.status == "VERIFIED" else "error",
            started_at=started,
            expected_interval_seconds=None,
        )
        health_storage.finish_latest_job_execution(
            settings,
            job_name=STORAGE_BACKUP_LOCK_JOB,
            status="ok" if report.status == "VERIFIED" else "error",
            error=summary,
        )
    except Exception:
        logger.debug("Backup audit record failed", exc_info=True)


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


def create_backup_confirmation(
    settings: Settings,
    *,
    operator_id: str,
) -> tuple[BackupConfirmation | None, BackupReport]:
    """
    Bind a short-lived confirmation for Create Backup.
    Does not create a backup file.
    """
    reason = reject_reason_for_backup_dir(
        settings.storage_backup_dir, database_path=settings.database_path
    )
    configured = reason is None and bool((settings.storage_backup_dir or "").strip())
    if not configured:
        report = BackupReport(
            status="UNAVAILABLE",
            configured=False,
            detail=f"backup unavailable: {reason or 'empty'}",
            future_retention_gate="blocked_until_verified_backup",
        )
        return None, _set_last(report)

    conflict = fresh_exclusive_lock_job(settings)
    if conflict is not None:
        report = BackupReport(
            status="BUSY",
            configured=True,
            detail=f"maintenance busy: {conflict}",
            future_retention_gate="blocked_until_verified_backup",
        )
        return None, _set_last(report)

    db_identity = compute_source_database_identity(settings.database_path)
    dir_identity = _backup_dir_identity(settings)
    preview_fp = hashlib.sha256(
        json.dumps(
            {
                "db": db_identity,
                "dir": dir_identity,
                "enabled": bool(settings.storage_backup_enabled),
                "label": settings.storage_backup_label,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    token = uuid.uuid4().hex
    confirmation = BackupConfirmation(
        token=token,
        operator_id=str(operator_id),
        database_identity=db_identity,
        backup_dir_identity=dir_identity,
        backup_enabled=bool(settings.storage_backup_enabled),
        backup_label_prefix=settings.storage_backup_label,
        expires_at=time.time() + settings.storage_backup_confirm_ttl_seconds,
        preview_fingerprint=preview_fp,
    )
    _CONFIRMATIONS[token] = confirmation
    report = BackupReport(
        status="UNAVAILABLE" if not settings.storage_backup_enabled else "INCOMPLETE",
        configured=True,
        detail=(
            "confirmation preview ready — create requires matching token"
            if settings.storage_backup_enabled
            else "backup disabled by configuration"
        ),
        future_retention_gate="blocked_until_verified_backup",
        mutation_proof={"confirmation_fingerprint": _token_fingerprint(token)},
    )
    if not settings.storage_backup_enabled:
        # Still allow confirmation binding so UI can show unavailable clearly;
        # execute will refuse.
        pass
    return confirmation, _set_last(report)


def execute_confirmed_backup(
    settings: Settings,
    *,
    token: str,
    operator_id: str,
) -> BackupReport:
    """Execute online backup only when confirmation binding still matches."""
    confirmation = _CONFIRMATIONS.get(token)
    if confirmation is None:
        return _set_last(
            BackupReport(
                status="REFUSED",
                configured=bool((settings.storage_backup_dir or "").strip()),
                detail="confirmation token unknown or already consumed",
                future_retention_gate="blocked_until_verified_backup",
                mutation_proof={"failure_code": "invalid_confirmation"},
            )
        )
    if time.time() > confirmation.expires_at:
        _CONFIRMATIONS.pop(token, None)
        report = BackupReport(
            status="REFUSED",
            configured=True,
            detail="confirmation token expired",
            future_retention_gate="blocked_until_verified_backup",
            mutation_proof={
                "failure_code": "confirmation_expired",
                "confirmation_fingerprint": _token_fingerprint(token),
            },
        )
        _record_backup_audit(settings, report)
        return _set_last(report)
    if confirmation.operator_id != str(operator_id):
        report = BackupReport(
            status="REFUSED",
            configured=True,
            detail="confirmation bound to a different operator",
            future_retention_gate="blocked_until_verified_backup",
            mutation_proof={
                "failure_code": "confirmation_operator_mismatch",
                "confirmation_fingerprint": _token_fingerprint(token),
            },
        )
        _record_backup_audit(settings, report)
        return _set_last(report)

    db_identity = compute_source_database_identity(settings.database_path)
    dir_identity = _backup_dir_identity(settings)
    if (
        db_identity != confirmation.database_identity
        or dir_identity != confirmation.backup_dir_identity
        or bool(settings.storage_backup_enabled) != confirmation.backup_enabled
        or settings.storage_backup_label != confirmation.backup_label_prefix
    ):
        _CONFIRMATIONS.pop(token, None)
        report = BackupReport(
            status="REFUSED",
            configured=True,
            detail="confirmation binding no longer matches configuration",
            future_retention_gate="blocked_until_verified_backup",
            mutation_proof={
                "failure_code": "stale_preview",
                "confirmation_fingerprint": _token_fingerprint(token),
            },
        )
        _record_backup_audit(settings, report)
        return _set_last(report)

    if not settings.storage_backup_enabled:
        _CONFIRMATIONS.pop(token, None)
        report = BackupReport(
            status="UNAVAILABLE",
            configured=True,
            detail="backup disabled by configuration",
            future_retention_gate="blocked_until_verified_backup",
            mutation_proof={"failure_code": "backup_disabled"},
        )
        _record_backup_audit(settings, report)
        return _set_last(report)

    conflict = fresh_exclusive_lock_job(settings)
    if conflict is not None:
        report = BackupReport(
            status="BUSY",
            configured=True,
            detail=f"maintenance busy: {conflict}",
            future_retention_gate="blocked_until_verified_backup",
            mutation_proof={
                "failure_code": "maintenance_busy",
                "confirmation_fingerprint": _token_fingerprint(token),
            },
        )
        _record_backup_audit(settings, report)
        return _set_last(report)

    # Single-use consume before create (repeated taps are idempotent refusals).
    _CONFIRMATIONS.pop(token, None)
    report = create_online_backup(settings)
    return report


def create_online_backup(settings: Settings) -> BackupReport:
    """
    Create an online SQLite backup with integrity verification.
    Never uses ordinary filesystem copy of the live DB.
    Never deletes backups. Never restores over live DB.
    """
    conflict = fresh_exclusive_lock_job(settings, exclude_job=STORAGE_BACKUP_LOCK_JOB)
    if conflict is not None:
        report = BackupReport(
            status="BUSY",
            configured=bool((settings.storage_backup_dir or "").strip()),
            detail=f"maintenance busy: {conflict}",
            future_retention_gate="blocked_until_verified_backup",
        )
        _record_backup_audit(settings, report)
        return _set_last(report)

    report = _create_online_backup_inner(settings)
    _record_backup_audit(settings, report)
    return report


def _create_online_backup_inner(settings: Settings) -> BackupReport:
    """Inner online backup implementation (lock + sqlite3.backup)."""
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
            "source_database_identity": compute_source_database_identity(str(db_path)),
            "source_database_basename": source_database_basename(str(db_path)),
            "integrity_check": details.get("integrity_check") or integrity,
            "quick_check": details.get("quick_check") or "ok",
            "required_tables_verified": not bool(details.get("missing_required")),
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


@dataclass(frozen=True)
class VerifiedBackupGate:
    ok: bool
    failure_code: str
    detail: str
    backup_path: Path | None = None
    backup_label: str | None = None
    created_at: str | None = None
    age_seconds: float | None = None
    integrity_result: str | None = None
    quick_check: str | None = None
    source_database_identity: str | None = None
    meta: dict = field(default_factory=dict)


def revalidate_verified_backup_for_pruning(
    settings: Settings,
    *,
    max_age_seconds: int | None = None,
) -> VerifiedBackupGate:
    """
    Re-read backup file + metadata from disk and re-run integrity checks.
    Never trusts in-memory status alone. Never mutates backups.
    """
    max_age = int(
        max_age_seconds
        if max_age_seconds is not None
        else getattr(settings, "storage_retention_prune_max_backup_age_seconds", 86400)
    )
    expected_identity = compute_source_database_identity(settings.database_path)

    reason = reject_reason_for_backup_dir(
        settings.storage_backup_dir, database_path=settings.database_path
    )
    if reason is not None:
        return VerifiedBackupGate(
            ok=False,
            failure_code="backup_unavailable",
            detail=f"backup unavailable: {reason}",
        )

    root = resolve_backup_dir(
        settings.storage_backup_dir, database_path=settings.database_path
    )
    if root is None or not root.exists():
        return VerifiedBackupGate(
            ok=False,
            failure_code="missing_backup",
            detail="no backup directory or backups present",
        )

    verified_files: list[tuple[float, Path, dict]] = []
    for path in root.glob("*.db"):
        if not backup_path_is_safe(
            path, backup_root=root, database_path=settings.database_path
        ):
            continue
        meta = _read_meta(path)
        if meta is None:
            continue
        if meta.get("status") != "VERIFIED":
            continue
        created = meta.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = path.stat().st_mtime
        verified_files.append((ts, path, meta))

    if not verified_files:
        # Distinguish missing meta vs no files vs unverified
        any_db = list(root.glob("*.db"))
        if not any_db:
            return VerifiedBackupGate(
                ok=False,
                failure_code="missing_backup",
                detail="no backup files found",
            )
        metas = [_read_meta(p) for p in any_db]
        if any(m is None for m in metas):
            return VerifiedBackupGate(
                ok=False,
                failure_code="missing_backup_metadata",
                detail="backup metadata missing or unreadable",
            )
        return VerifiedBackupGate(
            ok=False,
            failure_code="unverified_backup",
            detail="no VERIFIED backup on disk",
        )

    verified_files.sort(key=lambda item: item[0], reverse=True)
    ts, path, meta = verified_files[0]

    required_meta_keys = (
        "status",
        "created_at",
        "integrity_result",
        "source_database_identity",
        "label",
    )
    missing_keys = [k for k in required_meta_keys if not meta.get(k)]
    if missing_keys:
        return VerifiedBackupGate(
            ok=False,
            failure_code="missing_backup_metadata",
            detail=f"incomplete metadata: {','.join(missing_keys)}",
            backup_path=path,
            backup_label=sanitize_path(path.name),
            meta=dict(meta),
        )

    identity = str(meta.get("source_database_identity") or "")
    if identity != expected_identity:
        return VerifiedBackupGate(
            ok=False,
            failure_code="wrong_database_identity",
            detail="backup source identity does not match live database",
            backup_path=path,
            backup_label=sanitize_path(path.name),
            source_database_identity=identity,
            meta=dict(meta),
        )

    age = max(0.0, _now().timestamp() - ts)
    if age > max_age:
        return VerifiedBackupGate(
            ok=False,
            failure_code="expired_backup",
            detail="verified backup older than configured maximum age",
            backup_path=path,
            backup_label=sanitize_path(path.name),
            created_at=str(meta.get("created_at")),
            age_seconds=round(age, 1),
            source_database_identity=identity,
            meta=dict(meta),
        )

    # Confirm file still exists and re-verify integrity on disk.
    if not path.is_file() or not _meta_path(path).is_file():
        return VerifiedBackupGate(
            ok=False,
            failure_code="missing_backup",
            detail="backup file or metadata disappeared before prune",
            backup_label=sanitize_path(path.name),
        )

    status, integrity, details = _verify_backup_file(path)
    if status != "VERIFIED":
        code = "failed_integrity"
        if str(integrity).startswith("missing_tables"):
            code = "failed_required_tables"
        elif str(integrity).startswith("quick_check"):
            code = "failed_quick_check"
        return VerifiedBackupGate(
            ok=False,
            failure_code=code,
            detail=f"backup revalidation failed: {integrity}",
            backup_path=path,
            backup_label=sanitize_path(path.name),
            created_at=str(meta.get("created_at")),
            age_seconds=round(age, 1),
            integrity_result=integrity,
            quick_check=str(details.get("quick_check") or ""),
            source_database_identity=identity,
            meta=dict(meta),
        )

    quick = str(details.get("quick_check") or meta.get("quick_check") or "ok")
    if quick.lower() not in {"ok", "missing"}:
        return VerifiedBackupGate(
            ok=False,
            failure_code="failed_quick_check",
            detail=f"quick_check failed: {quick}",
            backup_path=path,
            backup_label=sanitize_path(path.name),
            integrity_result=integrity,
            quick_check=quick,
            source_database_identity=identity,
            meta=dict(meta),
        )

    if details.get("missing_required"):
        return VerifiedBackupGate(
            ok=False,
            failure_code="failed_required_tables",
            detail="required critical tables missing in backup",
            backup_path=path,
            backup_label=sanitize_path(path.name),
            integrity_result=integrity,
            quick_check=quick,
            source_database_identity=identity,
            meta=dict(meta),
        )

    return VerifiedBackupGate(
        ok=True,
        failure_code="",
        detail="verified backup gate passed",
        backup_path=path,
        backup_label=sanitize_path(path.name),
        created_at=str(meta.get("created_at")),
        age_seconds=round(age, 1),
        integrity_result=integrity,
        quick_check=quick,
        source_database_identity=identity,
        meta=dict(meta),
    )
