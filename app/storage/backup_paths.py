"""Backup directory path validation (V2.1.1 Phase 4A)."""

from __future__ import annotations

from pathlib import Path

from app.storage.paths import (
    _forbidden_system_roots,
    _is_filesystem_root,
    is_within_approved_root,
    path_escapes_via_link,
    project_root,
)


def reject_reason_for_backup_dir(
    configured: str,
    *,
    database_path: str,
    project: Path | None = None,
) -> str | None:
    """
    Validate explicitly configured backup directory.
    Empty => unavailable (not an error for optional config).
    Relative paths are rejected — absolute canonical path required.
    """
    if configured is None:
        return "empty"
    raw = str(configured).strip()
    if not raw:
        return "empty"
    if raw in {".", "..", "./", ".\\", "../", "..\\"}:
        return "ambiguous_relative"
    parts = Path(raw).parts
    if ".." in parts:
        return "path_traversal"
    if raw.startswith("\\\\") or raw.startswith("//"):
        return "unc_or_network"

    path = Path(raw)
    if not path.is_absolute():
        return "relative_not_allowed"

    project = (project or project_root()).resolve()
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return "unresolvable"

    if _is_filesystem_root(resolved):
        return "filesystem_root"
    if resolved == project:
        return "project_root"

    forbidden_children = {
        project / "app",
        project / "tests",
        project / "docs",
        project / ".git",
        project / ".venv",
        project / "venv",
        project / "logs",
        project / "data",  # ambiguous live DB area
    }
    # Also reject common configured cleanup roots if they resolve under project.
    for name in ("logs", "cache", "proof", "proof_artifacts"):
        forbidden_children.add(project / name)

    if resolved in forbidden_children:
        return "source_or_runtime_tree"
    # Nested under forbidden trees
    for banned in forbidden_children:
        try:
            resolved.relative_to(banned.resolve())
            return "inside_forbidden_tree"
        except (ValueError, OSError):
            pass

    for forbidden in _forbidden_system_roots():
        if resolved == forbidden:
            return "forbidden_system_path"

    # Reject live DB file path and its parent directory (sidecars / overwrite risk).
    if database_path and database_path != ":memory:":
        try:
            db = Path(database_path)
            db_resolved = db.resolve() if db.is_absolute() else (Path.cwd() / db).resolve()
            if resolved == db_resolved:
                return "live_database_path"
            if resolved == db_resolved.parent:
                return "live_database_directory"
            # Backup root must not be inside a path that is the live DB file (impossible for dirs)
            # but must not equal wal/shm parents same as db parent — already covered.
        except (OSError, RuntimeError):
            return "live_database_unresolvable"

    return None


def resolve_backup_dir(
    configured: str,
    *,
    database_path: str,
    project: Path | None = None,
) -> Path | None:
    reason = reject_reason_for_backup_dir(
        configured, database_path=database_path, project=project
    )
    if reason is not None:
        return None
    return Path(str(configured).strip()).resolve()


def backup_path_is_safe(
    candidate: Path,
    *,
    backup_root: Path,
    database_path: str,
) -> bool:
    """Ensure candidate stays inside backup root and never targets live DB sidecars."""
    if path_escapes_via_link(candidate, backup_root):
        return False
    if not is_within_approved_root(candidate, backup_root):
        return False
    if database_path and database_path != ":memory:":
        try:
            db = Path(database_path)
            db_resolved = db.resolve() if db.is_absolute() else (Path.cwd() / db).resolve()
            cand = candidate.resolve()
            if cand == db_resolved:
                return False
            for suffix in ("-wal", "-shm", "-journal"):
                if cand == Path(str(db_resolved) + suffix):
                    return False
        except (OSError, RuntimeError):
            return False
    return True
