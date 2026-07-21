"""Verified-backup-gated bounded SQLite retention pruning (V2.1.1 Phase 3B)."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config.constants import STORAGE_RETENTION_PRUNE_LOCK_JOB
from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.sanitize import logical_path_label, sanitize_path
from app.storage.backup import (
    VerifiedBackupGate,
    compute_source_database_identity,
    revalidate_verified_backup_for_pruning,
)
from app.storage.lock import acquire_prune_lock, release_prune_lock
from app.storage.retention_policies import (
    RetentionPolicy,
    build_retention_policies,
    validate_policy_registry,
)

logger = logging.getLogger(__name__)

_LAST_PRUNE_REPORT = None
_CONFIRMATIONS: dict[str, "PruneConfirmation"] = {}

_PROTECTED_COUNT_TABLES = (
    "users",
    "copy_watchlist",
    "wallet_monitor_state",
    "user_filters",
    "copy_user_settings",
    "processed_copy_events",
    "wallet_event_watermarks",
    "component_states",
    "copy_poll_state",
)

_FORBIDDEN_SQL = ("VACUUM", "journal_mode", "wal_checkpoint", "ATTACH", "DETACH")


@dataclass(frozen=True)
class PolicyPruneResult:
    dataset: str
    table: str
    status: str  # DELETED | DRY_RUN | SKIPPED | FAILED | REFUSED
    candidate_count: int = 0
    deleted_count: int = 0
    skipped_protected: int = 0
    batch_limit: int = 0
    cutoff_iso: str | None = None
    detail: str = ""
    rolled_back: bool = False


@dataclass
class PruneReport:
    status: str  # DRY_RUN | SUCCEEDED | FAILED | REFUSED
    dry_run: bool
    prune_enabled: bool
    approved_datasets: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    backup_label: str | None = None
    backup_age_seconds: float | None = None
    candidate_counts: dict[str, int] = field(default_factory=dict)
    deleted_counts: dict[str, int] = field(default_factory=dict)
    skipped_protected_counts: dict[str, int] = field(default_factory=dict)
    policy_results: list[PolicyPruneResult] = field(default_factory=list)
    failure_code: str = ""
    detail: str = ""
    rolled_back: bool = False
    operator_id: str | None = None
    confirmation_token_fingerprint: str | None = None
    mutation_proof: dict = field(default_factory=dict)
    sql_statements_executed: list[str] = field(default_factory=list)

    def as_sanitized_dict(self) -> dict:
        return {
            "prune_status": self.status,
            "prune_dry_run": self.dry_run,
            "prune_enabled": self.prune_enabled,
            "prune_approved_datasets": list(self.approved_datasets),
            "prune_started_at": self.started_at,
            "prune_finished_at": self.finished_at,
            "prune_backup_label": self.backup_label or "(none)",
            "prune_backup_age_seconds": self.backup_age_seconds,
            "prune_candidate_total": sum(self.candidate_counts.values()),
            "prune_deleted_total": sum(self.deleted_counts.values()),
            "prune_skipped_protected_total": sum(self.skipped_protected_counts.values()),
            "prune_failure_code": self.failure_code or None,
            "prune_detail": self.detail,
            "prune_rolled_back": self.rolled_back,
            "prune_operator_id": self.operator_id,
            "prune_path_label": logical_path_label("live_database"),
            "prune_note": "Row deletions only — SQLite file size is not reclaimed without manual VACUUM",
            "prune_mutation_proof": dict(self.mutation_proof),
        }


@dataclass
class PruneConfirmation:
    token: str
    operator_id: str
    database_identity: str
    datasets: tuple[str, ...]
    cutoffs: dict[str, str]
    candidate_counts: dict[str, int]
    backup_label: str
    backup_created_at: str
    expires_at: float
    preview_fingerprint: str


def get_last_prune_report() -> PruneReport | None:
    return _LAST_PRUNE_REPORT


def _set_last(report: PruneReport) -> PruneReport:
    global _LAST_PRUNE_REPORT
    _LAST_PRUNE_REPORT = report
    return report


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff_iso(days: int) -> str:
    # Second precision keeps confirmation bindings stable across sub-second gaps.
    return (_now() - timedelta(days=max(0, days))).replace(microsecond=0).isoformat()


def _now_iso() -> str:
    return _now().replace(microsecond=0).isoformat()


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _policies_by_dataset() -> dict[str, RetentionPolicy]:
    return {p.dataset: p for p in build_retention_policies()}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(r[1]) for r in rows}


def _count_table(connection: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(connection, table):
        return None
    try:
        row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return None


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    payload = json.dumps([(r[0], r[1], r[2]) for r in rows], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _journal_mode(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row else None
    except sqlite3.Error:
        return None


def _protected_counts(connection: sqlite3.Connection) -> dict[str, int | None]:
    return {name: _count_table(connection, name) for name in _PROTECTED_COUNT_TABLES}


def _candidate_where(
    policy: RetentionPolicy,
    *,
    now_iso: str,
) -> tuple[str, list[Any], str]:
    """
    Build WHERE clause + params + cutoff_iso for an APPROVED policy.
    Table/column identifiers come only from the registry.
    """
    assert policy.timestamp_column is not None
    assert policy.retention_days is not None
    ts = policy.timestamp_column

    if policy.table == "copy_job_locks" and ts == "expires_at":
        cutoff = now_iso
        where = f'"{ts}" < ?'
        return where, [cutoff], cutoff

    cutoff = _cutoff_iso(policy.retention_days)
    parts = [f'"{ts}" IS NOT NULL', f'"{ts}" < ?']
    params: list[Any] = [cutoff]
    if policy.terminal_predicate:
        parts.append(f"({policy.terminal_predicate})")
    if policy.protected_exclusion_predicate:
        parts.append(f"NOT ({policy.protected_exclusion_predicate})")
    return " AND ".join(parts), params, cutoff


def _assert_no_forbidden_sql(sql: str) -> None:
    upper = sql.upper()
    for token in _FORBIDDEN_SQL:
        if token in upper:
            raise RuntimeError(f"forbidden_sql:{token}")


def _open_connection(settings: Settings) -> sqlite3.Connection:
    db_path = Path(settings.database_path)
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    return connection


def _record_audit(settings: Settings, report: PruneReport) -> None:
    """Persist a sanitized prune outcome via existing job_executions table."""
    try:
        summary = json.dumps(
            {
                "status": report.status,
                "dry_run": report.dry_run,
                "failure_code": report.failure_code or None,
                "deleted_total": sum(report.deleted_counts.values()),
                "candidate_total": sum(report.candidate_counts.values()),
                "approved": list(report.approved_datasets),
                "rolled_back": report.rolled_back,
                "backup_label": report.backup_label,
                "operator_id": report.operator_id,
            },
            sort_keys=True,
        )
        # Keep payload free of wallets/paths/secrets.
        if any(x in summary.lower() for x in ("token", "wallet", ":\\", "/users/", "/home/")):
            summary = '{"status":"sanitized"}'
        health_storage.insert_job_execution(
            settings,
            job_name=STORAGE_RETENTION_PRUNE_LOCK_JOB,
            status="ok" if report.status in {"DRY_RUN", "SUCCEEDED"} else "error",
            started_at=report.started_at or _now_iso(),
            expected_interval_seconds=None,
        )
        health_storage.finish_latest_job_execution(
            settings,
            job_name=STORAGE_RETENTION_PRUNE_LOCK_JOB,
            status="ok" if report.status in {"DRY_RUN", "SUCCEEDED"} else "error",
            duration_ms=None,
            error=summary if report.status not in {"DRY_RUN", "SUCCEEDED"} else summary,
        )
    except Exception:
        logger.debug("Failed to persist prune audit record", exc_info=True)


def inventory_prune_candidates(
    settings: Settings,
    *,
    approved_datasets: list[str] | tuple[str, ...] | None,
    connection: sqlite3.Connection | None = None,
) -> tuple[dict[str, int], dict[str, str], list[str], list[PolicyPruneResult]]:
    """
    Count candidates for explicitly approved datasets after revalidation.
    Returns (candidate_counts, cutoffs, errors, policy_results).
    """
    registry = _policies_by_dataset()
    registry_errors = validate_policy_registry()
    if registry_errors:
        return {}, {}, [f"registry_invalid:{registry_errors[0]}"], []

    if not approved_datasets:
        return {}, {}, ["approved_policy_list_required"], []

    own_conn = connection is None
    conn = connection or _open_connection(settings)
    candidate_counts: dict[str, int] = {}
    cutoffs: dict[str, str] = {}
    errors: list[str] = []
    results: list[PolicyPruneResult] = []
    now_iso = _now_iso()

    try:
        for dataset in approved_datasets:
            policy = registry.get(dataset)
            if policy is None:
                errors.append(f"unknown_policy:{dataset}")
                results.append(
                    PolicyPruneResult(
                        dataset=dataset,
                        table="?",
                        status="REFUSED",
                        detail="unknown policy",
                    )
                )
                continue
            if policy.kind != "APPROVED":
                errors.append(f"protected_policy:{dataset}")
                results.append(
                    PolicyPruneResult(
                        dataset=dataset,
                        table=policy.table,
                        status="REFUSED",
                        detail="protected policy rejected",
                        skipped_protected=1,
                    )
                )
                continue
            if not _table_exists(conn, policy.table):
                errors.append(f"missing_table:{policy.table}")
                results.append(
                    PolicyPruneResult(
                        dataset=dataset,
                        table=policy.table,
                        status="REFUSED",
                        detail="table missing",
                    )
                )
                continue
            cols = _columns(conn, policy.table)
            if policy.timestamp_column and policy.timestamp_column not in cols:
                errors.append(f"missing_column:{policy.timestamp_column}")
                results.append(
                    PolicyPruneResult(
                        dataset=dataset,
                        table=policy.table,
                        status="REFUSED",
                        detail="timestamp column missing",
                    )
                )
                continue

            where, params, cutoff = _candidate_where(policy, now_iso=now_iso)
            sql = f'SELECT COUNT(*) FROM "{policy.table}" WHERE {where}'
            _assert_no_forbidden_sql(sql)
            row = conn.execute(sql, tuple(params)).fetchone()
            count = int(row[0]) if row else 0
            total = _count_table(conn, policy.table) or 0
            candidate_counts[dataset] = count
            cutoffs[dataset] = cutoff
            results.append(
                PolicyPruneResult(
                    dataset=dataset,
                    table=policy.table,
                    status="DRY_RUN",
                    candidate_count=count,
                    skipped_protected=max(0, total - count),
                    batch_limit=min(
                        policy.batch_size,
                        settings.storage_retention_prune_max_rows_per_policy,
                    ),
                    cutoff_iso=cutoff,
                    detail="candidate inventory",
                )
            )
    finally:
        if own_conn:
            conn.close()

    return candidate_counts, cutoffs, errors, results


def create_prune_confirmation(
    settings: Settings,
    *,
    operator_id: str,
    approved_datasets: list[str],
) -> tuple[PruneConfirmation | None, PruneReport]:
    """
    Dry-run preview that binds a short-lived confirmation token.
    Performs zero DELETE statements.
    """
    report = run_retention_prune(
        settings,
        approved_datasets=approved_datasets,
        dry_run=True,
        operator_id=str(operator_id),
        require_lock=True,
    )
    if report.status != "DRY_RUN":
        return None, report

    gate = revalidate_verified_backup_for_pruning(
        settings,
        max_age_seconds=settings.storage_retention_prune_max_backup_age_seconds,
    )
    if not gate.ok:
        report.status = "REFUSED"
        report.failure_code = gate.failure_code
        report.detail = gate.detail
        return None, _set_last(report)

    cutoffs = {r.dataset: (r.cutoff_iso or "") for r in report.policy_results}
    token = uuid.uuid4().hex
    preview_fp = hashlib.sha256(
        json.dumps(
            {
                "datasets": list(approved_datasets),
                "cutoffs": cutoffs,
                "candidates": dict(report.candidate_counts),
                "backup": gate.backup_label,
                "backup_created": gate.created_at,
                "db": compute_source_database_identity(settings.database_path),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    confirmation = PruneConfirmation(
        token=token,
        operator_id=str(operator_id),
        database_identity=compute_source_database_identity(settings.database_path),
        datasets=tuple(approved_datasets),
        cutoffs=cutoffs,
        candidate_counts=dict(report.candidate_counts),
        backup_label=gate.backup_label or "",
        backup_created_at=gate.created_at or "",
        expires_at=time.time() + settings.storage_retention_prune_confirm_ttl_seconds,
        preview_fingerprint=preview_fp,
    )
    _CONFIRMATIONS[token] = confirmation
    report.confirmation_token_fingerprint = _token_fingerprint(token)
    report.backup_label = gate.backup_label
    report.backup_age_seconds = gate.age_seconds
    report.detail = "confirmation preview ready — destructive prune requires matching token"
    return confirmation, _set_last(report)


def execute_confirmed_prune(
    settings: Settings,
    *,
    token: str,
    operator_id: str,
) -> PruneReport:
    """Execute destructive prune only when confirmation binding still matches."""
    confirmation = _CONFIRMATIONS.get(token)
    if confirmation is None:
        return _set_last(
            PruneReport(
                status="REFUSED",
                dry_run=False,
                prune_enabled=settings.storage_retention_prune_enabled,
                failure_code="invalid_confirmation",
                detail="confirmation token unknown or already consumed",
                operator_id=str(operator_id),
                started_at=_now_iso(),
                finished_at=_now_iso(),
            )
        )
    if time.time() > confirmation.expires_at:
        _CONFIRMATIONS.pop(token, None)
        return _set_last(
            PruneReport(
                status="REFUSED",
                dry_run=False,
                prune_enabled=settings.storage_retention_prune_enabled,
                failure_code="confirmation_expired",
                detail="confirmation token expired",
                operator_id=str(operator_id),
                confirmation_token_fingerprint=_token_fingerprint(token),
                started_at=_now_iso(),
                finished_at=_now_iso(),
            )
        )
    if confirmation.operator_id != str(operator_id):
        return _set_last(
            PruneReport(
                status="REFUSED",
                dry_run=False,
                prune_enabled=settings.storage_retention_prune_enabled,
                failure_code="confirmation_operator_mismatch",
                detail="confirmation bound to a different operator",
                operator_id=str(operator_id),
                confirmation_token_fingerprint=_token_fingerprint(token),
                started_at=_now_iso(),
                finished_at=_now_iso(),
            )
        )

    # Fresh inventory + gate; stale preview invalidates confirmation.
    candidate_counts, _cutoffs, errors, _results = inventory_prune_candidates(
        settings,
        approved_datasets=list(confirmation.datasets),
    )
    gate = revalidate_verified_backup_for_pruning(
        settings,
        max_age_seconds=settings.storage_retention_prune_max_backup_age_seconds,
    )
    identity = compute_source_database_identity(settings.database_path)
    if (
        errors
        or not gate.ok
        or identity != confirmation.database_identity
        or dict(candidate_counts) != confirmation.candidate_counts
        or (gate.backup_label or "") != confirmation.backup_label
        or (gate.created_at or "") != confirmation.backup_created_at
    ):
        _CONFIRMATIONS.pop(token, None)
        return _set_last(
            PruneReport(
                status="REFUSED",
                dry_run=False,
                prune_enabled=settings.storage_retention_prune_enabled,
                failure_code="stale_preview" if not errors and gate.ok else (
                    (errors[0].split(":", 1)[0] if errors else gate.failure_code)
                    or "stale_preview"
                ),
                detail="confirmation binding no longer matches current preview/gate",
                operator_id=str(operator_id),
                confirmation_token_fingerprint=_token_fingerprint(token),
                started_at=_now_iso(),
                finished_at=_now_iso(),
            )
        )

    # Single-use: consume before destructive run (repeated taps are idempotent refusals).
    _CONFIRMATIONS.pop(token, None)

    return run_retention_prune(
        settings,
        approved_datasets=list(confirmation.datasets),
        dry_run=False,
        operator_id=str(operator_id),
        require_lock=True,
        confirmation_token_fingerprint=_token_fingerprint(token),
    )


def _refuse(
    settings: Settings,
    *,
    code: str,
    detail: str,
    dry_run: bool,
    operator_id: str | None,
    approved: list[str] | None = None,
    started_at: str | None = None,
    gate: VerifiedBackupGate | None = None,
) -> PruneReport:
    report = PruneReport(
        status="REFUSED",
        dry_run=dry_run,
        prune_enabled=settings.storage_retention_prune_enabled,
        approved_datasets=list(approved or []),
        started_at=started_at or _now_iso(),
        finished_at=_now_iso(),
        failure_code=code,
        detail=detail,
        operator_id=operator_id,
        backup_label=gate.backup_label if gate else None,
        backup_age_seconds=gate.age_seconds if gate else None,
        mutation_proof={"delete_statements": 0},
    )
    _record_audit(settings, report)
    return _set_last(report)


def _delete_policy_batch(
    connection: sqlite3.Connection,
    settings: Settings,
    policy: RetentionPolicy,
    *,
    remaining_run_budget: int,
    now_iso: str,
    sql_log: list[str],
) -> PolicyPruneResult:
    """Delete one bounded oldest-first batch inside an open transaction."""
    where, params, cutoff = _candidate_where(policy, now_iso=now_iso)
    per_policy = min(
        policy.batch_size,
        settings.storage_retention_prune_max_rows_per_policy,
        remaining_run_budget,
    )
    if per_policy <= 0:
        return PolicyPruneResult(
            dataset=policy.dataset,
            table=policy.table,
            status="SKIPPED",
            detail="run row budget exhausted",
            cutoff_iso=cutoff,
        )

    select_sql = (
        f'SELECT rowid FROM "{policy.table}" WHERE {where} '
        f'ORDER BY "{policy.timestamp_column}" ASC, rowid ASC LIMIT ?'
    )
    select_params = tuple(params) + (per_policy,)
    _assert_no_forbidden_sql(select_sql)
    sql_log.append(select_sql)
    rows = connection.execute(select_sql, select_params).fetchall()
    rowids = [int(r[0]) for r in rows]
    if not rowids:
        return PolicyPruneResult(
            dataset=policy.dataset,
            table=policy.table,
            status="DELETED",
            candidate_count=0,
            deleted_count=0,
            batch_limit=per_policy,
            cutoff_iso=cutoff,
            detail="no eligible rows",
        )

    placeholders = ",".join("?" for _ in rowids)
    # Re-enforce terminal/cutoff predicates inside DELETE.
    delete_sql = (
        f'DELETE FROM "{policy.table}" WHERE rowid IN ({placeholders}) AND ({where})'
    )
    delete_params = tuple(rowids) + tuple(params)
    _assert_no_forbidden_sql(delete_sql)
    sql_log.append(delete_sql)
    cursor = connection.execute(delete_sql, delete_params)
    deleted = int(cursor.rowcount if cursor.rowcount is not None else 0)
    if deleted > len(rowids) or deleted > per_policy:
        raise RuntimeError("post_delete_mismatch:deleted_exceeds_approved")
    if deleted != len(rowids):
        # Some selected rows no longer matched predicates — treat as mismatch.
        raise RuntimeError("post_delete_mismatch:deleted_count_mismatch")

    return PolicyPruneResult(
        dataset=policy.dataset,
        table=policy.table,
        status="DELETED",
        candidate_count=len(rowids),
        deleted_count=deleted,
        batch_limit=per_policy,
        cutoff_iso=cutoff,
        detail="bounded batch committed pending verification",
    )


def run_retention_prune(
    settings: Settings,
    *,
    approved_datasets: list[str] | tuple[str, ...] | None,
    dry_run: bool | None = None,
    operator_id: str | None = None,
    require_lock: bool = True,
    confirmation_token_fingerprint: str | None = None,
    _simulate_post_delete_failure: bool = False,
) -> PruneReport:
    """
    Bounded retention prune gated by a revalidated VERIFIED backup.

    Dry-run (default): inventory only, zero DELETE.
    Destructive: requires enabled flag, dry_run=False, lock, verified backup,
    and an explicit approved policy list.
    """
    started = _now_iso()
    effective_dry_run = (
        settings.storage_retention_prune_dry_run if dry_run is None else bool(dry_run)
    )
    approved = list(approved_datasets or [])

    if not approved:
        return _refuse(
            settings,
            code="approved_policy_list_required",
            detail="explicit approved policy list required",
            dry_run=effective_dry_run,
            operator_id=operator_id,
            approved=approved,
            started_at=started,
        )

    if not effective_dry_run and not settings.storage_retention_prune_enabled:
        return _refuse(
            settings,
            code="prune_disabled",
            detail="retention pruning is disabled",
            dry_run=False,
            operator_id=operator_id,
            approved=approved,
            started_at=started,
        )

    if not effective_dry_run and settings.storage_retention_prune_dry_run and dry_run is None:
        # Settings still force dry-run unless caller explicitly overrides.
        effective_dry_run = True

    # Destructive path requires dry_run explicitly False AND settings dry_run False
    # when using defaults; callers may pass dry_run=False for tests after enabling.
    if not effective_dry_run and settings.storage_retention_prune_dry_run:
        return _refuse(
            settings,
            code="dry_run_required",
            detail="destructive prune refused while STORAGE_RETENTION_PRUNE_DRY_RUN=true",
            dry_run=True,
            operator_id=operator_id,
            approved=approved,
            started_at=started,
        )

    gate: VerifiedBackupGate | None = None
    if not effective_dry_run:
        gate = revalidate_verified_backup_for_pruning(
            settings,
            max_age_seconds=settings.storage_retention_prune_max_backup_age_seconds,
        )
        if not gate.ok:
            return _refuse(
                settings,
                code=gate.failure_code or "backup_gate_failed",
                detail=gate.detail,
                dry_run=False,
                operator_id=operator_id,
                approved=approved,
                started_at=started,
                gate=gate,
            )

    owner: str | None = None
    if require_lock:
        owner = acquire_prune_lock(settings)
        if owner is None:
            return _refuse(
                settings,
                code="lock_unavailable",
                detail="retention prune lock held",
                dry_run=effective_dry_run,
                operator_id=operator_id,
                approved=approved,
                started_at=started,
                gate=gate,
            )

    report = PruneReport(
        status="DRY_RUN" if effective_dry_run else "SUCCEEDED",
        dry_run=effective_dry_run,
        prune_enabled=settings.storage_retention_prune_enabled,
        approved_datasets=approved,
        started_at=started,
        operator_id=operator_id,
        confirmation_token_fingerprint=confirmation_token_fingerprint,
        backup_label=gate.backup_label if gate else None,
        backup_age_seconds=gate.age_seconds if gate else None,
    )

    connection: sqlite3.Connection | None = None
    try:
        connection = _open_connection(settings)
        deadline = time.monotonic() + settings.storage_retention_prune_timeout_seconds

        candidate_counts, cutoffs, errors, inventory_results = inventory_prune_candidates(
            settings,
            approved_datasets=approved,
            connection=connection,
        )
        report.candidate_counts = dict(candidate_counts)
        report.skipped_protected_counts = {
            r.dataset: r.skipped_protected for r in inventory_results if r.skipped_protected
        }
        report.policy_results = list(inventory_results)

        if errors:
            # Any policy validation error refuses the whole run (zero DELETE).
            report.status = "REFUSED"
            report.failure_code = errors[0].split(":", 1)[0]
            report.detail = errors[0]
            report.finished_at = _now_iso()
            report.mutation_proof = {"delete_statements": 0, "errors": errors}
            _record_audit(settings, report)
            return _set_last(report)

        if effective_dry_run:
            report.status = "DRY_RUN"
            report.detail = "dry-run inventory only — zero DELETE statements"
            report.finished_at = _now_iso()
            report.mutation_proof = {
                "delete_statements": 0,
                "cutoffs": cutoffs,
                "note": "logical retention preview; file size unchanged",
            }
            _record_audit(settings, report)
            return _set_last(report)

        # Revalidate backup immediately before destructive work (not memory-only).
        gate = revalidate_verified_backup_for_pruning(
            settings,
            max_age_seconds=settings.storage_retention_prune_max_backup_age_seconds,
        )
        if not gate.ok:
            report.status = "REFUSED"
            report.failure_code = gate.failure_code
            report.detail = gate.detail
            report.finished_at = _now_iso()
            report.mutation_proof = {"delete_statements": 0, "backup_revalidation": False}
            _record_audit(settings, report)
            return _set_last(report)
        report.backup_label = gate.backup_label
        report.backup_age_seconds = gate.age_seconds

        registry = _policies_by_dataset()
        journal_before = _journal_mode(connection)
        schema_before = _schema_fingerprint(connection)
        protected_before = _protected_counts(connection)
        sql_log: list[str] = []
        remaining = settings.storage_retention_prune_max_rows_per_run
        now_iso = _now_iso()
        deleted_counts: dict[str, int] = {}
        policy_results: list[PolicyPruneResult] = []

        # End any deferred read transaction before taking an exclusive write lock.
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        connection.execute("BEGIN IMMEDIATE")
        try:
            for dataset in approved:
                if time.monotonic() > deadline:
                    raise RuntimeError("execution_timeout")
                policy = registry[dataset]
                result = _delete_policy_batch(
                    connection,
                    settings,
                    policy,
                    remaining_run_budget=remaining,
                    now_iso=now_iso,
                    sql_log=sql_log,
                )
                policy_results.append(result)
                deleted_counts[dataset] = result.deleted_count
                remaining -= result.deleted_count
                if result.status == "FAILED":
                    raise RuntimeError(result.detail or "policy_failed")

            if _simulate_post_delete_failure:
                raise RuntimeError("post_delete_mismatch:simulated")

            # Post-delete verification before commit.
            protected_after = _protected_counts(connection)
            if protected_before != protected_after:
                raise RuntimeError("protected_table_count_changed")
            journal_after = _journal_mode(connection)
            if journal_before != journal_after:
                raise RuntimeError("journal_mode_changed")
            schema_after = _schema_fingerprint(connection)
            if schema_before != schema_after:
                raise RuntimeError("schema_changed")
            total_deleted = sum(deleted_counts.values())
            if total_deleted > settings.storage_retention_prune_max_rows_per_run:
                raise RuntimeError("post_delete_mismatch:run_limit_exceeded")
            for stmt in sql_log:
                _assert_no_forbidden_sql(stmt)

            connection.execute("COMMIT")
            report.status = "SUCCEEDED"
            report.deleted_counts = deleted_counts
            report.policy_results = policy_results
            report.detail = (
                f"deleted {total_deleted} rows across {len(approved)} policies "
                "(logical retention only; VACUUM not run)"
            )
            report.mutation_proof = {
                "delete_statements": sum(1 for s in sql_log if s.startswith("DELETE")),
                "protected_counts_unchanged": True,
                "journal_mode_unchanged": True,
                "schema_unchanged": True,
                "no_vacuum": True,
                "backup_untouched": True,
                "backup_label": gate.backup_label,
            }
            report.sql_statements_executed = list(sql_log)
        except Exception as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            report.status = "FAILED"
            report.rolled_back = True
            report.failure_code = str(exc).split(":", 1)[0][:80]
            report.detail = f"rolled back: {type(exc).__name__}"
            report.deleted_counts = {}
            report.policy_results = [
                PolicyPruneResult(
                    dataset=r.dataset,
                    table=r.table,
                    status="FAILED",
                    candidate_count=r.candidate_count,
                    deleted_count=0,
                    rolled_back=True,
                    detail=str(exc),
                    cutoff_iso=r.cutoff_iso,
                )
                for r in policy_results
            ] or policy_results
            report.mutation_proof = {
                "delete_statements": 0,
                "rolled_back": True,
                "no_vacuum": True,
            }
            if not settings.storage_retention_prune_stop_on_policy_failure:
                # Default is stop; non-default continue is not implemented for safety.
                pass

        report.finished_at = _now_iso()
        _record_audit(settings, report)
        return _set_last(report)
    finally:
        if connection is not None:
            connection.close()
        if owner is not None:
            release_prune_lock(settings, owner)


def prune_metrics_for_health(settings: Settings) -> dict:
    """Sanitized prune status for infrastructure probe."""
    last = get_last_prune_report()
    base = {
        "prune_enabled": bool(settings.storage_retention_prune_enabled),
        "prune_dry_run_default": bool(settings.storage_retention_prune_dry_run),
        "prune_last_status": last.status if last else "none",
        "prune_last_deleted_total": sum(last.deleted_counts.values()) if last else 0,
        "prune_path_label": logical_path_label("live_database"),
        "prune_backup_gate": "required_verified_backup",
    }
    if last:
        base["prune_last_failure_code"] = last.failure_code or None
    # Never include absolute paths.
    for value in base.values():
        if isinstance(value, str):
            assert "Users" not in value
    _ = sanitize_path  # keep sanitizer import used for future labels
    return base
