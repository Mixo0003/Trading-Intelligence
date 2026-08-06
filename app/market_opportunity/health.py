"""Health probe helpers for Market Opportunity Intelligence (V4.2)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from app.config.settings import Settings
from app.market_opportunity.detectors.registry import enabled_detectors
from app.market_opportunity.freshness import (
    MAX_AGE_EIA_DAILY_SPOT_DAYS,
    MAX_AGE_EIA_WEEKLY_INVENTORY_DAYS,
    MAX_AGE_FRED_DAILY_CROSSCHECK_DAYS,
)
from app.market_opportunity.paper import MarketOpportunitySafetyError, assert_paper_only


def _parse_period(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _asof_days(period: date | None, *, now: datetime) -> int | None:
    if period is None:
        return None
    return (now.astimezone(timezone.utc).date() - period).days


def _sub_state(
    *,
    healthy: bool,
    degraded: bool = False,
    down: bool = False,
    detail: str,
) -> dict[str, Any]:
    if down:
        state = "DOWN"
    elif degraded:
        state = "DEGRADED"
    elif healthy:
        state = "HEALTHY"
    else:
        state = "DEGRADED"
    return {"state": state, "detail": detail}


def _price_period_from_diag(diag: dict[str, Any]) -> date | None:
    raw_price = diag.get("wti_price")
    if isinstance(raw_price, dict):
        return _parse_period(raw_price.get("period"))
    return _parse_period(diag.get("price_period") or diag.get("period"))


def _inventory_period_from_diag(diag: dict[str, Any]) -> date | None:
    raw_inv = diag.get("wti_inventory")
    if isinstance(raw_inv, dict):
        return _parse_period(raw_inv.get("period"))
    return _parse_period(diag.get("inventory_period"))


def _is_warming_before_first_refresh(diag: dict[str, Any], price_period: date | None) -> bool:
    """True when enabled MOI has not yet recorded an EIA price refresh attempt.

    Health monitor runs before the first MOI price job; treating that gap as
    EIA DOWN caused false Telegram alerts. Absence of a ``wti_price`` key means
    warmup — not a confirmed source failure.
    """
    if price_period is not None:
        return False
    if "wti_price" in diag:
        return False
    if diag.get("price_period"):
        return False
    return True


def evaluate_moi_health(
    settings: Settings,
    *,
    diagnostics: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Return a probe snapshot dict with WTI subcomponent metrics.

    - Disabled module → DISABLED (not DOWN)
    - Before first EIA refresh → DEGRADED warmup (not Telegram-alertable)
    - Missing FRED → DEGRADED (System Health only; not Telegram-alertable alone)
    - Stale/missing EIA price after observation → DOWN
    - Stale inventory (price ok) → DEGRADED
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)

    metrics: dict[str, Any] = {
        "enabled": settings.market_opportunity_enabled,
        "auto_alerts": settings.market_opportunity_auto_alerts_enabled,
        "read_only": settings.market_opportunity_read_only,
        "enabled_detectors": list(enabled_detectors()),
        "warming": False,
        "telegram_alert_policy": "standard",
    }

    if not settings.market_opportunity_enabled:
        disabled = _sub_state(healthy=False, detail="module disabled")
        disabled["state"] = "DISABLED"
        metrics.update(
            {
                "wti_price": dict(disabled),
                "wti_inventory": dict(disabled),
                "fred_crosscheck": dict(disabled),
                "detector_runtime": dict(disabled),
                "paper_outcome_runtime": dict(disabled),
                "telegram_alert_policy": "suppress",
            }
        )
        return {
            "state": "DISABLED",
            "detail": "Market Opportunity Intelligence disabled",
            "probe_ok": True,
            "hard_down": False,
            "metrics": metrics,
        }

    try:
        assert_paper_only(settings)
    except MarketOpportunitySafetyError as exc:
        down = _sub_state(healthy=False, down=True, detail=str(exc))
        metrics.update(
            {
                "wti_price": dict(down),
                "wti_inventory": dict(down),
                "fred_crosscheck": dict(down),
                "detector_runtime": dict(down),
                "paper_outcome_runtime": dict(down),
                "telegram_alert_policy": "standard",
            }
        )
        return {
            "state": "DOWN",
            "detail": str(exc),
            "probe_ok": False,
            "hard_down": True,
            "metrics": metrics,
        }

    diag = dict(diagnostics or {})
    if not diag:
        diag = _load_runtime_diagnostics(settings)

    price_period = _price_period_from_diag(diag)
    inv_period = _inventory_period_from_diag(diag)
    price_age = _asof_days(price_period, now=clock)
    inv_age = _asof_days(inv_period, now=clock)
    warming = _is_warming_before_first_refresh(diag, price_period)
    metrics["warming"] = warming

    # wti_price
    if warming:
        wti_price = _sub_state(
            healthy=False,
            degraded=True,
            detail="awaiting first EIA RWTC refresh",
        )
    elif price_period is None or price_age is None:
        wti_price = _sub_state(
            healthy=False, down=True, detail="EIA RWTC missing"
        )
    elif price_age > MAX_AGE_EIA_DAILY_SPOT_DAYS:
        wti_price = _sub_state(
            healthy=False,
            down=True,
            detail=f"EIA RWTC stale as_of_age={price_age}d",
        )
    else:
        wti_price = _sub_state(
            healthy=True, detail=f"EIA RWTC as_of={price_period.isoformat()}"
        )

    # wti_inventory — stale is DEGRADED (not hard-down for whole MOI)
    if inv_period is None or inv_age is None:
        wti_inventory = _sub_state(
            healthy=False, degraded=True, detail="EIA inventory missing"
        )
    elif inv_age > MAX_AGE_EIA_WEEKLY_INVENTORY_DAYS:
        wti_inventory = _sub_state(
            healthy=False,
            degraded=True,
            detail=f"EIA inventory stale as_of_age={inv_age}d",
        )
    else:
        wti_inventory = _sub_state(
            healthy=True, detail=f"EIA inventory as_of={inv_period.isoformat()}"
        )

    # fred_crosscheck — missing/stale = DEGRADED, never hard DOWN alone
    fred_info = diag.get("fred_crosscheck") or {}
    fred_ok = bool(fred_info.get("ok")) if isinstance(fred_info, dict) else False
    fred_period = _parse_period(
        fred_info.get("period") if isinstance(fred_info, dict) else None
    )
    fred_age = _asof_days(fred_period, now=clock)
    if diag.get("fred_conflict"):
        fred_crosscheck = _sub_state(
            healthy=False, degraded=True, detail="FRED same-period conflict"
        )
    elif not fred_ok or fred_period is None:
        fred_crosscheck = _sub_state(
            healthy=False, degraded=True, detail="FRED missing or unavailable"
        )
    elif fred_age is not None and fred_age > MAX_AGE_FRED_DAILY_CROSSCHECK_DAYS:
        fred_crosscheck = _sub_state(
            healthy=False,
            degraded=True,
            detail=f"FRED stale as_of_age={fred_age}d",
        )
    else:
        fred_crosscheck = _sub_state(healthy=True, detail="FRED same-period ok")

    detector_info = diag.get("detector_runtime") or {}
    if isinstance(detector_info, dict) and detector_info.get("ok") is False:
        detector_runtime = _sub_state(
            healthy=False,
            down=True,
            detail=str(detector_info.get("error") or "detector runtime failure"),
        )
    elif isinstance(detector_info, dict) and detector_info.get("ok") is True:
        detector_runtime = _sub_state(
            healthy=True,
            detail=(
                f"scan ok published={detector_info.get('published', 0)} "
                f"suppressed={detector_info.get('suppressed', 0)}"
            ),
        )
    else:
        detector_runtime = _sub_state(
            healthy=True, degraded=True, detail="detector runtime not yet observed"
        )

    outcome_info = diag.get("paper_outcome_runtime") or {}
    if isinstance(outcome_info, dict) and outcome_info.get("ok") is False:
        paper_outcome_runtime = _sub_state(
            healthy=False,
            down=True,
            detail=str(outcome_info.get("error") or "outcome runtime failure"),
        )
    elif isinstance(outcome_info, dict) and outcome_info.get("ok") is True:
        paper_outcome_runtime = _sub_state(
            healthy=True,
            detail=f"outcomes ok measured={outcome_info.get('measured', 0)}",
        )
    else:
        paper_outcome_runtime = _sub_state(
            healthy=True, degraded=True, detail="outcome runtime not yet observed"
        )

    metrics.update(
        {
            "wti_price": wti_price,
            "wti_inventory": wti_inventory,
            "fred_crosscheck": fred_crosscheck,
            "detector_runtime": detector_runtime,
            "paper_outcome_runtime": paper_outcome_runtime,
        }
    )

    # Overall rollup
    sub_states = [
        wti_price["state"],
        wti_inventory["state"],
        fred_crosscheck["state"],
        detector_runtime["state"],
        paper_outcome_runtime["state"],
    ]
    if warming:
        overall = "DEGRADED"
        hard_down = False
        detail = wti_price["detail"]
        probe_ok = True
        metrics["telegram_alert_policy"] = "suppress"
    elif wti_price["state"] == "DOWN" or detector_runtime["state"] == "DOWN":
        overall = "DOWN"
        hard_down = wti_price["state"] == "DOWN"
        detail = (
            wti_price["detail"]
            if wti_price["state"] == "DOWN"
            else detector_runtime["detail"]
        )
        probe_ok = False
        metrics["telegram_alert_policy"] = (
            "eia" if wti_price["state"] == "DOWN" else "standard"
        )
    elif "DOWN" in sub_states:
        overall = "DOWN"
        hard_down = False
        detail = "MOI subcomponent DOWN"
        probe_ok = False
        metrics["telegram_alert_policy"] = "standard"
    elif "DEGRADED" in sub_states:
        overall = "DEGRADED"
        hard_down = False
        if fred_crosscheck["state"] == "DEGRADED":
            detail = fred_crosscheck["detail"]
        elif wti_inventory["state"] == "DEGRADED":
            detail = wti_inventory["detail"]
        else:
            detail = "MOI degraded"
        probe_ok = True
        # Soft degrade with healthy EIA: System Health DEGRADED, no Telegram.
        if wti_price["state"] == "HEALTHY":
            metrics["telegram_alert_policy"] = "suppress"
        else:
            metrics["telegram_alert_policy"] = "standard"
    else:
        overall = "HEALTHY"
        hard_down = False
        detail = "Market Opportunity Intelligence WTI path healthy"
        probe_ok = True
        metrics["telegram_alert_policy"] = "eia"

    return {
        "state": overall,
        "detail": detail,
        "probe_ok": probe_ok,
        "hard_down": hard_down,
        "metrics": metrics,
    }


def _load_runtime_diagnostics(settings: Settings) -> dict[str, Any]:
    try:
        from app.database import market_opportunity_storage as moi_storage

        snap = moi_storage.get_runtime_status(settings)
    except Exception:
        return {}
    if not snap:
        return {}
    detail = snap.get("detail")
    if isinstance(detail, dict):
        return detail
    return {"runtime_status": snap.get("status"), "detail": detail}
