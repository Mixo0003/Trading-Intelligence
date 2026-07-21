"""Path validation for explicit-root storage cleanup (V2.1.1 Phase 2)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Logical labels only — never emit absolute private paths in reports.
CLEANUP_ROOT_LABELS = frozenset({"logs", "cache", "proof_artifacts"})

_PROTECTED_BASENAMES = frozenset(
    {
        ".env",
        ".env.example",
        ".gitignore",
        ".git",
    }
)


def project_root() -> Path:
    """Repository root (app/storage/paths.py -> parents[2])."""
    return Path(__file__).resolve().parents[2]


def _is_filesystem_root(path: Path) -> bool:
    resolved = path.resolve()
    return resolved.parent == resolved


def _forbidden_system_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        roots.append(Path.home().resolve())
    except OSError:
        pass
    try:
        roots.append((Path.home() / "Downloads").resolve())
    except OSError:
        pass
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except OSError:
        pass
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{drive}:/")
        if candidate.exists():
            try:
                roots.append(candidate.resolve())
            except OSError:
                continue
    roots.append(Path("/").resolve())
    return roots


def reject_reason_for_configured_root(configured: str, *, project: Path | None = None) -> str | None:
    """
    Return a rejection reason if the configured cleanup root is unsafe/invalid.
    Empty/unset configuration is not an error — it means no cleanup for that root.
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

    project = (project or project_root()).resolve()
    try:
        path = Path(raw)
        resolved = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    except (OSError, RuntimeError):
        return "unresolvable"

    if _is_filesystem_root(resolved):
        return "filesystem_root"
    if resolved == project:
        return "project_root"
    # Reject cleaning the entire repo source tree by pointing at app/, tests/, etc.
    try:
        if resolved in {project / "app", project / "tests", project / "docs", project / ".git"}:
            return "source_or_git_tree"
    except OSError:
        pass

    for forbidden in _forbidden_system_roots():
        if resolved == forbidden:
            return "forbidden_system_path"
    return None


def resolve_approved_root(configured: str, *, project: Path | None = None) -> Path | None:
    """Return resolved root Path if valid and configured; None if unset or rejected."""
    reason = reject_reason_for_configured_root(configured, project=project)
    if reason == "empty":
        return None
    if reason is not None:
        return None
    path = Path(str(configured).strip())
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def is_within_approved_root(candidate: Path, root: Path) -> bool:
    """True when candidate resolves strictly inside root (symlink-escape safe)."""
    try:
        cand = candidate.resolve()
        base = root.resolve()
        cand.relative_to(base)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def protected_database_paths(database_path: str) -> set[Path]:
    """Live DB and journal sidecars — never delete."""
    if not database_path or database_path == ":memory:":
        return set()
    try:
        db = Path(database_path)
        resolved = db.resolve() if db.is_absolute() else (Path.cwd() / db).resolve()
    except (OSError, RuntimeError):
        return set()
    return {
        resolved,
        Path(str(resolved) + "-wal"),
        Path(str(resolved) + "-shm"),
        Path(str(resolved) + "-journal"),
    }


def is_protected_file(
    path: Path,
    *,
    database_path: str,
    active_log_path: Path | None,
) -> bool:
    name = path.name
    if name in _PROTECTED_BASENAMES:
        return True
    if name.startswith(".env"):
        return True
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return True
    for protected in protected_database_paths(database_path):
        try:
            if resolved == protected.resolve():
                return True
        except (OSError, RuntimeError):
            if resolved == protected:
                return True
        # Also protect by basename match for DB sidecars inside cleanup roots.
        if name == protected.name:
            return True
    if active_log_path is not None:
        try:
            if resolved == active_log_path.resolve():
                return True
        except (OSError, RuntimeError):
            if path.name == active_log_path.name:
                return True
    return False


def path_escapes_via_link(path: Path, root: Path) -> bool:
    """Detect symlink/junction whose resolved target escapes the approved root."""
    try:
        if path.is_symlink() or _is_junction(path):
            return not is_within_approved_root(path, root)
        # If any parent is a link escaping root, resolve will leave root.
        return not is_within_approved_root(path, root) and path.exists()
    except OSError:
        return True


def _is_junction(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        # Windows reparse points / junctions often report as symlink or directory.
        st = path.lstat()
        # FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & 0x400) and not path.is_symlink()
    except (OSError, AttributeError):
        return False
