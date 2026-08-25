from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests

from app.opportunity_intel.discovery import (
    CompleteDiscoveryCache,
    DiscoveryResult,
    ResultStatus,
    discover_complete_markets,
    shortlist_outcomes,
)
from app.opportunity_intel.models import (
    ObservationMode,
    OpportunityDiagnosticCode,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _market(
    market_id: str = "market-1",
    *,
    condition_id: str = "condition-1",
    tokens: list[str] | None = None,
    outcomes: list[str] | None = None,
    prices: list[str] | None = None,
    live: bool = False,
    liquidity: object = "10000",
    volume_24h: object = "25000",
    updated_at: str | None = None,
    end_at: str | None = None,
    **overrides,
) -> dict:
    value = {
        "id": market_id,
        "conditionId": condition_id,
        "question": f"Will {market_id} happen?",
        "active": True,
        "closed": False,
        "archived": False,
        "resolved": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "live": live,
        "liquidityNum": liquidity,
        "volume24hr": volume_24h,
        "updatedAt": updated_at or (NOW - timedelta(seconds=30)).isoformat(),
        "endDate": end_at or (NOW + timedelta(days=1)).isoformat(),
        "outcomes": outcomes if outcomes is not None else ["Yes", "No"],
        "clobTokenIds": tokens if tokens is not None else [f"{market_id}-yes", f"{market_id}-no"],
        "outcomePrices": prices if prices is not None else ["0.55", "0.45"],
    }
    value.update(overrides)
    return value


def _event(
    event_id: str = "event-1",
    *,
    markets: list[dict] | None = None,
    live: bool = False,
    tags: list[dict] | None = None,
    **overrides,
) -> dict:
    value = {
        "id": event_id,
        "slug": f"{event_id}-question",
        "title": f"{event_id} title",
        "active": True,
        "closed": False,
        "archived": False,
        "resolved": False,
        "live": live,
        "updatedAt": (NOW - timedelta(seconds=30)).isoformat(),
        "endDate": (NOW + timedelta(days=1)).isoformat(),
        "tags": tags if tags is not None else [{"id": "politics", "slug": "politics"}],
        "markets": markets if markets is not None else [_market()],
    }
    value.update(overrides)
    return value


def _client(*pages):
    client = MagicMock()
    client.get_json.side_effect = list(pages)
    return client


def _complete_market(raw_event: dict | None = None):
    result = discover_complete_markets(
        _client({"events": [raw_event or _event()], "next_cursor": None}),
        now=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    assert len(result.markets) == 1
    return result.markets[0]


def _http_error(status_code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    error = requests.exceptions.HTTPError(f"HTTP {status_code}")
    error.response = response
    return error


def test_single_page_completion_and_no_offset():
    client = _client({"events": [_event()], "next_cursor": None})
    result = discover_complete_markets(client, now=NOW)
    assert result.status == ResultStatus.COMPLETE
    assert result.pages_fetched == 1
    assert len(result.markets) == 1
    params = client.get_json.call_args.kwargs["params"]
    assert params == {"closed": "false", "limit": 500}
    assert "offset" not in params


def test_multipage_completion_follows_cursor_and_orders_deterministically():
    client = _client(
        {
            "events": [
                _event(
                    "event-2",
                    markets=[_market("market-2", condition_id="condition-2")],
                )
            ],
            "next_cursor": "cursor-a",
        },
        {"events": [_event("event-1", markets=[_market("market-1")])], "next_cursor": None},
    )
    result = discover_complete_markets(client, now=NOW)
    assert result.status == ResultStatus.COMPLETE
    assert result.pages_fetched == 2
    assert [market.event_id for market in result.markets] == ["event-1", "event-2"]
    assert client.get_json.call_args_list[1].kwargs["params"]["after_cursor"] == "cursor-a"
    assert all(
        "offset" not in call.kwargs["params"]
        for call in client.get_json.call_args_list
    )


def test_empty_page_is_normal_cursor_termination():
    result = discover_complete_markets(
        _client(
            {"events": [_event()], "next_cursor": "cursor-a"},
            {"events": [], "next_cursor": "unused"},
        ),
        now=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    assert result.pages_fetched == 2


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (requests.exceptions.ReadTimeout("timeout"), ResultStatus.RETRYABLE_FAILURE),
        (_http_error(503), ResultStatus.RETRYABLE_FAILURE),
        (_http_error(400), ResultStatus.PERMANENT_FAILURE),
    ],
)
def test_page_request_failure_is_typed_and_never_returns_partial(exception, expected):
    client = MagicMock()
    client.get_json.side_effect = [
        {"events": [_event()], "next_cursor": "cursor-a"},
        exception,
    ]
    result = discover_complete_markets(client, now=NOW)
    assert result.status == expected
    assert result.markets == ()
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.DISCOVERY_PAGE_FAILED


def test_repeated_cursor_invalidates_universe():
    result = discover_complete_markets(
        _client(
            {"events": [_event()], "next_cursor": "loop"},
            {"events": [_event("event-2", markets=[_market("market-2")])], "next_cursor": "loop"},
        ),
        now=NOW,
    )
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.markets == ()
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.DISCOVERY_CURSOR_REPEAT


@pytest.mark.parametrize("cursor", ["", "bad cursor", "../cursor", "x" * 513, 123])
def test_malformed_cursor_invalidates_universe(cursor):
    result = discover_complete_markets(
        _client({"events": [_event()], "next_cursor": cursor}),
        now=NOW,
    )
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.DISCOVERY_CURSOR_INVALID


def test_page_cap_exhaustion_invalidates_universe():
    result = discover_complete_markets(
        _client(
            {"events": [_event()], "next_cursor": "cursor-a"},
            {"events": [_event("event-2", markets=[_market("market-2")])], "next_cursor": "cursor-b"},
        ),
        now=NOW,
        max_pages=2,
    )
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.markets == ()
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.DISCOVERY_PAGE_CAP


def test_failed_refresh_does_not_replace_complete_cache_and_expired_cache_rejects():
    cache = CompleteDiscoveryCache(ttl_seconds=300, catalog_ttl_seconds=600)
    complete = discover_complete_markets(
        _client({"events": [_event()], "next_cursor": None}),
        now=NOW,
    )
    stored = cache.refresh(lambda: complete, now=NOW)
    assert stored.status == ResultStatus.COMPLETE
    failure = DiscoveryResult(
        status=ResultStatus.RETRYABLE_FAILURE,
        markets=(),
        diagnostics=(),
        pages_fetched=0,
        completed_at=None,
    )
    retained = cache.refresh(lambda: failure, now=NOW + timedelta(minutes=4))
    assert retained.status == ResultStatus.COMPLETE
    assert retained.from_cache is True
    assert retained.refresh_failure_status == ResultStatus.RETRYABLE_FAILURE
    # Soft discovery TTL exceeded, but catalog identity TTL still usable.
    soft = cache.refresh(lambda: failure, now=NOW + timedelta(seconds=301))
    assert soft.status == ResultStatus.COMPLETE
    assert soft.refresh_failure_status == ResultStatus.RETRYABLE_FAILURE
    # Hard catalog identity expiry drops the trusted universe.
    expired = cache.refresh(lambda: failure, now=NOW + timedelta(seconds=601))
    assert expired.status == ResultStatus.RETRYABLE_FAILURE
    assert expired.markets == ()


def test_duplicate_event_identity_invalidates_universe():
    result = discover_complete_markets(
        _client({"events": [_event("event-1"), _event("event-1")], "next_cursor": None}),
        now=NOW,
    )
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.DUPLICATE_EVENT


def test_duplicate_market_identity_invalidates_universe():
    result = discover_complete_markets(
        _client(
            {
                "events": [
                    _event("event-1", markets=[_market("market-shared")]),
                    _event("event-2", markets=[_market("market-shared")]),
                ],
                "next_cursor": None,
            }
        ),
        now=NOW,
    )
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.DUPLICATE_MARKET


def test_duplicate_token_identity_invalidates_universe():
    first = _market("market-1", tokens=["shared-token", "token-1-no"])
    second = _market("market-2", condition_id="condition-2", tokens=["shared-token", "token-2-no"])
    result = discover_complete_markets(
        _client({"events": [_event(markets=[first, second])], "next_cursor": None}),
        now=NOW,
    )
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.DUPLICATE_ASSET


@pytest.mark.parametrize(
    ("event_changes", "market_changes", "expected_code"),
    [
        ({"active": False}, {}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({"closed": True}, {}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({"archived": True}, {}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({"resolved": True}, {}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({}, {"active": False}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({}, {"closed": True}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({}, {"archived": True}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({}, {"resolved": True}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({}, {"acceptingOrders": False}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({}, {"enableOrderBook": False}, OpportunityDiagnosticCode.MARKET_STATUS_INVALID),
        ({"id": None}, {}, OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID),
        ({}, {"id": None}, OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID),
        ({}, {"conditionId": None}, OpportunityDiagnosticCode.MARKET_IDENTITY_INVALID),
        ({"slug": "https://evil.example/x"}, {}, OpportunityDiagnosticCode.LINK_SLUG_INVALID),
        ({}, {"outcomes": ["Yes"], "clobTokenIds": ["yes", "no"]}, OpportunityDiagnosticCode.OUTCOME_TOKEN_MISMATCH),
        ({}, {"liquidityNum": "NaN"}, OpportunityDiagnosticCode.MARKET_NUMERIC_INVALID),
        ({}, {"volume24hr": "not-number"}, OpportunityDiagnosticCode.MARKET_NUMERIC_INVALID),
        ({}, {"endDate": "not-a-time"}, OpportunityDiagnosticCode.MARKET_TIME_INVALID),
        ({}, {"updatedAt": "2026-07-24"}, OpportunityDiagnosticCode.MARKET_TIME_INVALID),
        ({"tags": [{"id": "../bad"}]}, {}, OpportunityDiagnosticCode.MARKET_CATEGORY_INVALID),
    ],
)
def test_invalid_market_or_event_is_quarantined(
    event_changes, market_changes, expected_code
):
    market = _market(**market_changes)
    event = _event(markets=[market], **event_changes)
    result = discover_complete_markets(
        _client({"events": [event], "next_cursor": None}),
        now=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    assert result.markets == ()
    assert result.diagnostics[0].code == expected_code


def test_malformed_market_isolated_while_healthy_market_survives():
    invalid = _market("market-bad", condition_id="condition-bad", liquidity="bad")
    valid = _market("market-good", condition_id="condition-good")
    result = discover_complete_markets(
        _client({"events": [_event(markets=[invalid, valid])], "next_cursor": None}),
        now=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    assert [market.market_id for market in result.markets] == ["market-good"]
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.MARKET_NUMERIC_INVALID


def test_normal_and_live_classification_are_explicit():
    normal = _complete_market()
    live = _complete_market(
        _event(
            "event-live",
            live=True,
            markets=[
                _market(
                    "market-live",
                    condition_id="condition-live",
                    live=True,
                    liquidity="25000",
                    volume_24h="50000",
                )
            ],
        )
    )
    assert normal.mode == ObservationMode.NORMAL
    assert live.mode == ObservationMode.LIVE_STRICT

    missing_flags = _event()
    missing_flags.pop("live")
    missing_flags["markets"][0].pop("live")
    result = discover_complete_markets(
        _client({"events": [missing_flags], "next_cursor": None}),
        now=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    assert len(result.markets) == 1
    assert result.markets[0].mode == ObservationMode.NORMAL


def test_gamma_tag_slug_is_used_instead_of_numeric_transport_id():
    market = _complete_market(
        _event(tags=[{"id": "100265", "slug": "geopolitics"}])
    )
    assert market.category_ids == ("geopolitics",)


def test_shortlist_round_robins_categories_without_changing_family_rank():
    sports = _complete_market(
        _event(
            tags=[{"id": "1", "slug": "sports"}],
            markets=[_market(volume_24h="100000")],
        )
    )
    geopolitics = replace(
        sports,
        event_id="event-geo",
        market_id="market-geo",
        condition_id="condition-geo",
        category_ids=("geopolitics",),
        volume_24h_usd=Decimal("50000"),
        outcomes=tuple(
            replace(outcome, asset_id=f"geo-{outcome.index}")
            for outcome in sports.outcomes
        ),
    )
    weather = replace(
        sports,
        event_id="event-weather",
        market_id="market-weather",
        condition_id="condition-weather",
        category_ids=("weather",),
        volume_24h_usd=Decimal("25000"),
        outcomes=tuple(
            replace(outcome, asset_id=f"weather-{outcome.index}")
            for outcome in sports.outcomes
        ),
    )
    selected = shortlist_outcomes(
        [sports, geopolitics, weather],
        now=NOW,
        strict_live_demand=False,
        max_candidates=4,
    )
    assert [candidate.category_ids for candidate in selected] == [
        ("sports",),
        ("geopolitics",),
        ("weather",),
        ("sports",),
    ]


def test_normal_shortlist_balanced_thresholds_and_price_hint():
    market = _complete_market()
    selected = shortlist_outcomes(
        [market],
        now=NOW,
        strict_live_demand=False,
    )
    assert [candidate.asset_id for candidate in selected] == [
        "market-1-yes",
        "market-1-no",
    ]
    below_liquidity = replace(market, liquidity_usd=Decimal("9999.99"))
    assert shortlist_outcomes(
        [below_liquidity], now=NOW, strict_live_demand=False
    ) == ()
    bad_hint = replace(
        market,
        outcomes=(replace(market.outcomes[0], price_hint=Decimal("0.01")),),
    )
    assert shortlist_outcomes([bad_hint], now=NOW, strict_live_demand=False) == ()


def test_live_shortlist_requires_demand_and_never_uses_normal_thresholds():
    strict_market = _complete_market(
        _event(
            "event-live",
            live=True,
            markets=[
                _market(
                    "market-live",
                    condition_id="condition-live",
                    live=True,
                    liquidity="25000",
                    volume_24h="50000",
                )
            ],
        )
    )
    assert shortlist_outcomes(
        [strict_market], now=NOW, strict_live_demand=False
    ) == ()
    assert len(
        shortlist_outcomes([strict_market], now=NOW, strict_live_demand=True)
    ) == 2

    normal_only_thresholds = replace(
        strict_market,
        liquidity_usd=Decimal("10000"),
        volume_24h_usd=Decimal("25000"),
    )
    assert shortlist_outcomes(
        [normal_only_thresholds], now=NOW, strict_live_demand=True
    ) == ()


def test_shortlist_preserves_stale_updated_at_and_enforces_time_remaining():
    normal = _complete_market()
    # Gamma updatedAt age is provenance only — stale ages must not reject Stage A.
    stale = replace(normal, metadata_at=NOW - timedelta(hours=2))
    selected = shortlist_outcomes([stale], now=NOW, strict_live_demand=False)
    assert [candidate.asset_id for candidate in selected] == [
        "market-1-yes",
        "market-1-no",
    ]
    assert all(candidate.metadata_at == stale.metadata_at for candidate in selected)
    assert shortlist_outcomes(
        [replace(normal, end_at=NOW + timedelta(hours=5, minutes=59))],
        now=NOW,
        strict_live_demand=False,
    ) == ()
    # Future provenance timestamps remain unsafe and are skipped.
    assert shortlist_outcomes(
        [replace(normal, metadata_at=NOW + timedelta(seconds=1))],
        now=NOW,
        strict_live_demand=False,
    ) == ()


def test_live_shortlist_does_not_reject_stale_source_updated_at():
    strict_market = _complete_market(
        _event(
            "event-live",
            live=True,
            markets=[
                _market(
                    "market-live",
                    condition_id="condition-live",
                    live=True,
                    liquidity="25000",
                    volume_24h="50000",
                    updated_at=(NOW - timedelta(hours=3)).isoformat(),
                )
            ],
        )
    )
    selected = shortlist_outcomes(
        [strict_market], now=NOW, strict_live_demand=True
    )
    assert len(selected) == 2
    assert selected[0].metadata_at == NOW - timedelta(hours=3)


def test_expired_and_low_remaining_markets_rejected_before_publication():
    expired = discover_complete_markets(
        _client(
            {
                "events": [
                    _event(
                        markets=[
                            _market(end_at=(NOW - timedelta(seconds=1)).isoformat())
                        ]
                    )
                ],
                "next_cursor": None,
            }
        ),
        now=NOW,
    )
    assert expired.status == ResultStatus.COMPLETE
    assert expired.markets == ()
    assert expired.diagnostics[0].code == OpportunityDiagnosticCode.MARKET_EXPIRED

    exactly_now = discover_complete_markets(
        _client(
            {
                "events": [
                    _event(markets=[_market(end_at=NOW.isoformat())])
                ],
                "next_cursor": None,
            }
        ),
        now=NOW,
    )
    assert exactly_now.status == ResultStatus.COMPLETE
    assert exactly_now.markets == ()
    assert exactly_now.diagnostics[0].code == OpportunityDiagnosticCode.MARKET_EXPIRED

    low_remaining = discover_complete_markets(
        _client(
            {
                "events": [
                    _event(
                        markets=[
                            _market(
                                end_at=(NOW + timedelta(hours=5)).isoformat()
                            )
                        ]
                    )
                ],
                "next_cursor": None,
            }
        ),
        now=NOW,
    )
    assert low_remaining.status == ResultStatus.COMPLETE
    assert low_remaining.markets == ()
    assert (
        low_remaining.diagnostics[0].code
        == OpportunityDiagnosticCode.MARKET_TIME_REMAINING_LOW
    )


def test_complete_proof_publishes_tradeable_subset_excluding_expired():
    client = _client(
        {
            "events": [
                _event(
                    "event-ok",
                    markets=[
                        _market(
                            "market-ok",
                            condition_id="condition-ok",
                            end_at=(NOW + timedelta(days=2)).isoformat(),
                        )
                    ],
                ),
                _event(
                    "event-expired",
                    markets=[
                        _market(
                            "market-expired",
                            condition_id="condition-expired",
                            end_at=(NOW - timedelta(days=30)).isoformat(),
                        )
                    ],
                ),
            ],
            "next_cursor": None,
        }
    )
    result = discover_complete_markets(client, now=NOW)
    assert result.status == ResultStatus.COMPLETE
    assert result.pages_fetched == 1
    assert len(result.markets) == 1
    assert result.markets[0].market_id == "market-ok"
    assert any(
        d.code == OpportunityDiagnosticCode.MARKET_EXPIRED for d in result.diagnostics
    )


def test_future_updated_at_rejected_as_time_invalid_not_metadata_stale():
    result = discover_complete_markets(
        _client(
            {
                "events": [
                    _event(
                        markets=[
                            _market(
                                updated_at=(NOW + timedelta(minutes=5)).isoformat()
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
    assert result.diagnostics[0].code == OpportunityDiagnosticCode.MARKET_TIME_INVALID
    assert all(
        d.code != OpportunityDiagnosticCode.METADATA_STALE for d in result.diagnostics
    )


def test_shortlist_caps_at_100_and_has_stable_activity_first_ties():
    base = _complete_market()
    markets = []
    for index in range(60):
        outcomes = tuple(
            replace(outcome, asset_id=f"asset-{index}-{outcome.index}")
            for outcome in base.outcomes
        )
        markets.append(
            replace(
                base,
                event_id=f"event-{index:03}",
                market_id=f"market-{index:03}",
                condition_id=f"condition-{index:03}",
                outcomes=outcomes,
                volume_24h_usd=Decimal("25000") + index,
            )
        )
    selected = shortlist_outcomes(markets, now=NOW, strict_live_demand=False)
    assert len(selected) == 100
    assert selected[0].event_id == "event-059"
    assert selected[-1].event_id == "event-010"
    assert selected[0].outcome_index == 0
    assert selected[1].outcome_index == 1


def test_per_user_filtering_does_not_rescan():
    client = _client({"events": [_event()], "next_cursor": None})
    result = discover_complete_markets(client, now=NOW)
    assert client.get_json.call_count == 1
    shortlist_outcomes(result.markets, now=NOW, strict_live_demand=False)
    shortlist_outcomes(result.markets, now=NOW, strict_live_demand=True)
    assert client.get_json.call_count == 1
