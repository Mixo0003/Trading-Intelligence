import json
from typing import Any

from app.polymarket.models import PolymarketEvent, PolymarketMarket


def _parse_jsonish(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def parse_market(raw: dict[str, Any]) -> PolymarketMarket:
    return PolymarketMarket(
        id=str(raw.get("id") or raw.get("conditionId") or raw.get("marketId") or "") or None,
        slug=raw.get("slug"),
        question=raw.get("question") or raw.get("title"),
        description=raw.get("description") or raw.get("resolutionSource") or "",
        rules=raw.get("rules") or raw.get("resolutionRules") or "",
        outcomes=_parse_jsonish(raw.get("outcomes") or raw.get("outcomeNames")),
        outcome_prices=_parse_jsonish(raw.get("outcomePrices") or raw.get("prices")),
        volume=_safe_float(raw.get("volume") or raw.get("volumeNum")),
        liquidity=_safe_float(raw.get("liquidity") or raw.get("liquidityNum")),
        end_date=raw.get("endDate") or raw.get("end_date_iso") or raw.get("end_date"),
        raw=raw,
    )


def parse_event(raw: dict[str, Any]) -> PolymarketEvent:
    raw_markets = raw.get("markets") or []
    markets = [
        parse_market(item)
        for item in raw_markets
        if isinstance(item, dict)
    ]

    return PolymarketEvent(
        id=str(raw.get("id") or "") or None,
        slug=raw.get("slug"),
        title=raw.get("title") or raw.get("question"),
        description=raw.get("description") or "",
        markets=markets,
        raw=raw,
    )
