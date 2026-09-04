"""Post-event missed-opportunity diagnostics. Not user spam."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from app.config.settings import Settings
from app.database import live_edge_storage
from app.opportunity_intel.live_edge.policy import MINIMUM_CONSERVATIVE_EDGE_PP


def diagnose_missed_window(
    settings: Settings,
    *,
    market_id: str,
    question: str,
    samples: tuple[tuple[Decimal, Decimal, bool], ...],
    alert_created: bool,
    now: datetime | None = None,
) -> str | None:
    """samples: (conservative_edge_pp, expected_value_usd, liquidity_ok)."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window = [
        index
        for index, (edge, ev, liquid) in enumerate(samples)
        if edge >= MINIMUM_CONSERVATIVE_EDGE_PP and ev > 0 and liquid
    ]
    if not window:
        return None
    if alert_created:
        return None
    reason = (
        "ПРОПУЩЕНА МОЖЛИВІСТЬ: існувало вікно з conservative edge ≥ порогу, "
        "додатним EV і достатньою ліквідністю, але alert не було створено"
    )
    live_edge_storage.record_missed(
        settings,
        market_id=market_id,
        question=question,
        reason=reason,
        window_json=json.dumps(
            {
                "sample_indexes": window,
                "threshold_edge_pp": str(MINIMUM_CONSERVATIVE_EDGE_PP),
            },
            separators=(",", ":"),
        ),
        created_at=clock,
    )
    return reason
