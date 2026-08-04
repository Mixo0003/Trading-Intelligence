"""updatedAt provenance semantics and expired end_at publication guards."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.opportunity_intel.discovery import (
    ResultStatus,
    discover_complete_markets,
    shortlist_outcomes,
)
from app.opportunity_intel.models import OpportunityDiagnosticCode

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _market(**overrides) -> dict:
    value = {
        "id": "market-1",
        "conditionId": "condition-1",
        "question": "Will market-1 happen?",
        "active": True,
        "closed": False,
        "archived": False,
        "resolved": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "live": False,
        "liquidityNum": "10000",
        "volume24hr": "25000",
        "updatedAt": (NOW - timedelta(seconds=30)).isoformat(),
        "endDate": (NOW + timedelta(days=1)).isoformat(),
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["market-1-yes", "market-1-no"],
        "outcomePrices": ["0.55", "0.45"],
    }
    value.update(overrides)
    return value


def _event(**overrides) -> dict:
    value = {
        "id": "event-1",
        "slug": "event-1-question",
        "title": "event-1 title",
        "active": True,
        "closed": False,
        "archived": False,
        "resolved": False,
        "live": False,
        "updatedAt": (NOW - timedelta(seconds=30)).isoformat(),
        "endDate": (NOW + timedelta(days=1)).isoformat(),
        "tags": [{"id": "politics", "slug": "politics"}],
        "markets": [_market()],
    }
    value.update(overrides)
    return value


def _client(payload: dict) -> MagicMock:
    client = MagicMock()
    client.get_json.side_effect = [payload]
    return client


def test_stale_gamma_updated_at_does_not_block_stage_a():
    result = discover_complete_markets(
        _client(
            {
                "events": [
                    _event(
                        markets=[
                            _market(
                                updatedAt=(NOW - timedelta(days=14)).isoformat()
                            )
                        ]
                    )
                ],
                "next_cursor": None,
            }
        ),
        now=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    assert len(result.markets) == 1
    assert result.markets[0].metadata_at == NOW - timedelta(days=14)
    selected = shortlist_outcomes(
        result.markets, now=NOW, strict_live_demand=False
    )
    assert len(selected) == 2
    assert all(d.code != OpportunityDiagnosticCode.METADATA_STALE for d in result.diagnostics)


def test_timezone_aware_expired_end_date_z_suffix_rejected():
    result = discover_complete_markets(
        _client(
            {
                "events": [
                    _event(
                        markets=[
                            _market(
                                endDate=(NOW - timedelta(hours=1))
                                .isoformat()
                                .replace("+00:00", "Z")
                            )
                        ]
                    )
                ],
                "next_cursor": None,
            }
        ),
        now=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    assert result.markets == ()
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.MARKET_EXPIRED


def test_future_end_at_with_sufficient_remaining_accepted():
    end = NOW + timedelta(hours=7)
    result = discover_complete_markets(
        _client(
            {
                "events": [_event(markets=[_market(endDate=end.isoformat())])],
                "next_cursor": None,
            }
        ),
        now=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    assert len(result.markets) == 1
    assert result.markets[0].end_at == end


def test_shortlist_keeps_provenance_metadata_at_on_candidates():
    result = discover_complete_markets(
        _client(
            {
                "events": [
                    _event(
                        markets=[
                            _market(
                                updatedAt=(NOW - timedelta(hours=6)).isoformat()
                            )
                        ]
                    )
                ],
                "next_cursor": None,
            }
        ),
        now=NOW,
    )
    selected = shortlist_outcomes(
        result.markets, now=NOW, strict_live_demand=False
    )
    assert selected[0].metadata_at == NOW - timedelta(hours=6)
    # Mutating candidate provenance is not required; ensure age gate absent.
    older = replace(result.markets[0], metadata_at=NOW - timedelta(days=40))
    assert shortlist_outcomes([older], now=NOW, strict_live_demand=False)
    assert older.liquidity_usd == Decimal("10000")
