"""Stable incident fingerprints and alert keys (no volatile free text)."""

from __future__ import annotations

import hashlib
import re

_REASON_RE = re.compile(r"[^a-z0-9_]+")


def normalize_reason_code(raw: str | None, *, fallback: str = "unspecified") -> str:
    text = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    text = _REASON_RE.sub("_", text).strip("_")
    if not text:
        text = fallback
    return text[:64]


def incident_fingerprint(component_id: str, reason_code: str) -> str:
    return f"{component_id}:{normalize_reason_code(reason_code)}"


def open_alert_key(*, fingerprint: str, incident_id: int, reason_code: str) -> str:
    return f"{fingerprint}:open:{incident_id}:{normalize_reason_code(reason_code)}"


def recovery_alert_key(*, fingerprint: str, incident_id: int) -> str:
    return f"{fingerprint}:resolved:{incident_id}"


def notified_open_marker_key(*, fingerprint: str, incident_id: int) -> str:
    """Marker that a DOWN open alert was actually delivered (enables recovery)."""
    return f"{fingerprint}:notified_open:{incident_id}"


def correlation_code(*, incident_id: int, reason_code: str) -> str:
    digest = hashlib.sha256(
        f"{incident_id}:{normalize_reason_code(reason_code)}".encode("utf-8")
    ).hexdigest()
    return digest[:8].upper()
