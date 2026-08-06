"""Checked-in UTC blackout schedule for V4.2 WTI Opportunity Intelligence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_BLACKOUT_PATH = (
    Path(__file__).resolve().parent / "calendars" / "wti_blackouts_v42.json"
)

REQUIRED_KINDS = ("WPSR", "CPI", "FOMC")
DEFAULT_POLICY: dict[str, dict[str, int]] = {
    "WPSR": {"before_hours": 2, "after_hours": 2},
    "CPI": {"before_hours": 1, "after_hours": 2},
    "FOMC": {"before_hours": 2, "after_hours": 3},
}

# WPSR is expected weekly on Thursday 10:30 America/New_York (= 14:30 UTC in EDT).
_WPSR_ANCHOR_UTC_HOUR = 14
_WPSR_ANCHOR_UTC_MINUTE = 30
_WPSR_LOOKBACK_DAYS = 7
_WPSR_LOOKAHEAD_DAYS = 30


class BlackoutError(ValueError):
    """Blackout schedule load or coverage failure (fail closed)."""


@dataclass(frozen=True)
class BlackoutEvent:
    id: str
    kind: str
    start_utc: datetime
    end_utc: datetime
    note: str = ""
    anchor_utc: datetime | None = None


@dataclass(frozen=True)
class BlackoutSchedule:
    timezone_display: str
    policy: dict[str, dict[str, int]]
    events: tuple[BlackoutEvent, ...]
    path: Path


def _require_aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BlackoutError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_utc(raw: Any, field_name: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise BlackoutError(f"{field_name} missing")
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BlackoutError(f"invalid {field_name}: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise BlackoutError(f"{field_name} must include UTC offset")
    return parsed.astimezone(timezone.utc)


def _expand_from_anchor(
    anchor: datetime,
    *,
    kind: str,
    policy: dict[str, dict[str, int]],
) -> tuple[datetime, datetime]:
    rules = policy.get(kind) or DEFAULT_POLICY.get(kind)
    if not rules:
        raise BlackoutError(f"no blackout policy for kind {kind!r}")
    before = int(rules.get("before_hours", 0))
    after = int(rules.get("after_hours", 0))
    return anchor - timedelta(hours=before), anchor + timedelta(hours=after)


def _parse_event(
    raw: dict[str, Any],
    *,
    policy: dict[str, dict[str, int]],
) -> BlackoutEvent:
    event_id = str(raw.get("id") or "").strip()
    kind = str(raw.get("kind") or "").strip().upper()
    if not event_id or not kind:
        raise BlackoutError("blackout event requires id and kind")

    anchor: datetime | None = None
    if raw.get("anchor_utc"):
        anchor = _parse_utc(raw.get("anchor_utc"), "anchor_utc")

    if raw.get("start_utc") and raw.get("end_utc"):
        start = _parse_utc(raw.get("start_utc"), "start_utc")
        end = _parse_utc(raw.get("end_utc"), "end_utc")
    elif anchor is not None:
        start, end = _expand_from_anchor(anchor, kind=kind, policy=policy)
    else:
        raise BlackoutError(
            f"event {event_id!r} needs start_utc/end_utc or anchor_utc"
        )

    if end <= start:
        raise BlackoutError(f"event {event_id!r} end_utc must be after start_utc")

    return BlackoutEvent(
        id=event_id,
        kind=kind,
        start_utc=start,
        end_utc=end,
        note=str(raw.get("note") or ""),
        anchor_utc=anchor,
    )


def load_blackout_schedule(path: Path | None = None) -> BlackoutSchedule:
    """Load and validate the checked-in WTI blackout JSON."""
    schedule_path = Path(path) if path is not None else DEFAULT_BLACKOUT_PATH
    try:
        payload = json.loads(schedule_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BlackoutError(f"blackout schedule missing: {schedule_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BlackoutError(f"blackout schedule unreadable: {exc}") from exc

    if not isinstance(payload, dict):
        raise BlackoutError("blackout schedule root must be an object")

    policy_raw = payload.get("policy") or {}
    if not isinstance(policy_raw, dict):
        raise BlackoutError("blackout policy must be an object")
    policy: dict[str, dict[str, int]] = {}
    for kind, rules in {**DEFAULT_POLICY, **policy_raw}.items():
        if not isinstance(rules, dict):
            raise BlackoutError(f"policy for {kind!r} must be an object")
        policy[str(kind).upper()] = {
            "before_hours": int(rules.get("before_hours", 0)),
            "after_hours": int(rules.get("after_hours", 0)),
        }

    events_raw = payload.get("events")
    if not isinstance(events_raw, list) or not events_raw:
        raise BlackoutError("blackout events must be a non-empty list")

    events = tuple(
        _parse_event(row, policy=policy)
        for row in events_raw
        if isinstance(row, dict)
    )
    if not events:
        raise BlackoutError("no valid blackout events parsed")

    return BlackoutSchedule(
        timezone_display=str(payload.get("timezone_display") or "America/New_York"),
        policy=policy,
        events=events,
        path=schedule_path,
    )


@lru_cache(maxsize=4)
def _cached_schedule(path_str: str) -> BlackoutSchedule:
    return load_blackout_schedule(Path(path_str))


def clear_schedule_cache() -> None:
    """Test helper: drop cached schedule loads."""
    _cached_schedule.cache_clear()


def get_schedule(path: Path | None = None) -> BlackoutSchedule:
    schedule_path = Path(path) if path is not None else DEFAULT_BLACKOUT_PATH
    return _cached_schedule(str(schedule_path.resolve()))


def is_in_blackout(
    now_utc: datetime,
    *,
    path: Path | None = None,
    schedule: BlackoutSchedule | None = None,
) -> bool:
    """Return True if ``now_utc`` falls inside any expanded blackout window."""
    clock = _require_aware_utc(now_utc, "now_utc")
    sched = schedule if schedule is not None else get_schedule(path)
    for event in sched.events:
        if event.start_utc <= clock < event.end_utc:
            return True
    return False


def _thursdays_between(start_day: date, end_day: date) -> list[date]:
    if end_day < start_day:
        return []
    days: list[date] = []
    cursor = start_day
    # weekday: Monday=0 ... Thursday=3
    while cursor.weekday() != 3:
        cursor += timedelta(days=1)
        if cursor > end_day:
            return days
    while cursor <= end_day:
        days.append(cursor)
        cursor += timedelta(days=7)
    return days


def _expected_wpsr_window(
    thursday: date,
    *,
    policy: dict[str, dict[str, int]],
) -> tuple[datetime, datetime]:
    anchor = datetime(
        thursday.year,
        thursday.month,
        thursday.day,
        _WPSR_ANCHOR_UTC_HOUR,
        _WPSR_ANCHOR_UTC_MINUTE,
        tzinfo=timezone.utc,
    )
    return _expand_from_anchor(anchor, kind="WPSR", policy=policy)


def _event_covers_window(
    event: BlackoutEvent,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """True if event overlaps the expected WPSR window for that Thursday."""
    if event.kind != "WPSR":
        return False
    return event.start_utc < window_end and event.end_utc > window_start


def assert_schedule_coverage(
    now_utc: datetime,
    *,
    path: Path | None = None,
    schedule: BlackoutSchedule | None = None,
    lookback_days: int = _WPSR_LOOKBACK_DAYS,
    lookahead_days: int = _WPSR_LOOKAHEAD_DAYS,
) -> None:
    """Fail closed if a required WPSR Thursday lacks a covering event.

    Scans Thursdays in ``[now - lookback, now + lookahead]`` and requires each
    to have a WPSR event overlapping the expected ±policy window around 14:30 UTC.
    Also requires the schedule to declare policy for WPSR/CPI/FOMC.
    """
    clock = _require_aware_utc(now_utc, "now_utc")
    sched = schedule if schedule is not None else get_schedule(path)

    for kind in REQUIRED_KINDS:
        if kind not in sched.policy:
            raise BlackoutError(f"blackout policy missing required kind {kind}")

    start_day = (clock - timedelta(days=lookback_days)).date()
    end_day = (clock + timedelta(days=lookahead_days)).date()
    missing: list[str] = []
    for thursday in _thursdays_between(start_day, end_day):
        window_start, window_end = _expected_wpsr_window(
            thursday, policy=sched.policy
        )
        if not any(
            _event_covers_window(event, window_start, window_end)
            for event in sched.events
        ):
            missing.append(thursday.isoformat())

    if missing:
        raise BlackoutError(
            "WPSR schedule coverage missing for Thursday(s): "
            + ", ".join(missing)
        )
