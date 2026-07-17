"""Regression tests for the stale-event substitution gap in event expansion.

Root cause: `expand_group_children` / `supplement_discovered_markets` re-fetch
sibling markets by slug via `get_event_by_slug` and replace the already
eligibility-checked children outright, without re-validating that the
re-fetched event actually matches the original event (same id) or is itself
still open/active/within the forecast horizon. If Polymarket ever returns a
different (stale, closed, out-of-horizon) event for the same slug, that stale
event's markets silently replace the correct ones and flow, unvalidated, into
`analyze_temperature_event` / the final Telegram card.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.polymarket.event_expansion import expand_group_children
from app.polymarket.markets import _collect_markets_from_gamma_discovery
from app.polymarket.models import PolymarketMarket
from app.polymarket.parser import parse_event

PARIS_RULES = (
    "Resolved using Weather Underground at LFPB station. "
    "https://www.wunderground.com/history/daily/fr/paris/LFPB"
)

# Discovery cache freshness uses UTC calendar day; keep fixture endDate in the
# future so this guard test does not fail solely because wall-clock UTC rolled.
_FRESH_END = (
    datetime.now(timezone.utc).date() + timedelta(days=2)
).isoformat() + "T12:00:00Z"

ORIGINAL_EVENT = {
    "id": "701548",
    "slug": "highest-temperature-in-paris-on-july-16-2026",
    "title": "Highest temperature in Paris on July 16?",
    "closed": False,
    "active": True,
    "endDate": _FRESH_END,
}


def _july_child(market_id: str, question: str, prices: list[float]) -> PolymarketMarket:
    return PolymarketMarket(
        id=market_id,
        slug=f"paris-july-16-{market_id}",
        question=question,
        description="",
        rules=PARIS_RULES,
        outcomes=["Yes", "No"],
        outcome_prices=prices,
        volume=None,
        liquidity=None,
        end_date="2026-07-16T12:00:00Z",
        raw={"events": [ORIGINAL_EVENT]},
    )


ORIGINAL_JULY_CHILDREN = [
    _july_child("h_tail", "Will the highest temperature in Paris be 29°C or below on July 16?", [0.008, 0.992]),
    _july_child("h31", "Will the highest temperature in Paris be 31°C on July 16?", [0.25, 0.75]),
    _july_child("h36", "Will the highest temperature in Paris be 36°C or higher on July 16?", [0.01, 0.99]),
]


def _stale_may_event():
    """A closed, out-of-horizon event returned for the *same* slug as above."""
    return parse_event({
        "id": "999999",
        "slug": "highest-temperature-in-paris-on-july-16-2026",
        "title": "Highest temperature in Paris on May 16?",
        "closed": True,
        "active": False,
        "endDate": "2026-05-16T12:00:00Z",
        "markets": [
            {
                "id": "stale1",
                "slug": "stale-highest-temperature-in-paris-on-may-16-2026",
                "question": "Will the highest temperature in Paris be 31°C on May 16?",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [0.4, 0.6],
                "endDate": "2026-05-16T12:00:00Z",
            }
        ],
    })


class TestExpandGroupChildrenRejectsStaleSubstitution:
    def test_mismatched_reexpanded_event_id_does_not_leak_into_result(self):
        """A re-fetch returning a different event id for the same slug must be rejected."""
        client = MagicMock()
        stale_event = _stale_may_event()

        with patch("app.polymarket.markets.get_event_by_slug", return_value=stale_event):
            result = expand_group_children(client, ORIGINAL_JULY_CHILDREN)

        result_questions = [m.question or "" for m in result]
        assert not any("may" in q.lower() for q in result_questions), (
            f"Stale May markets leaked into expand_group_children result: {result_questions}"
        )
        assert result == ORIGINAL_JULY_CHILDREN

    def test_matching_reexpanded_event_id_is_accepted(self):
        """Sanity check: when the re-fetched event id matches, expansion still works."""
        client = MagicMock()
        matching_event = parse_event({
            "id": "701548",
            "slug": "highest-temperature-in-paris-on-july-16-2026",
            "title": "Highest temperature in Paris on July 16?",
            "closed": False,
            "active": True,
            "endDate": "2026-07-16T12:00:00Z",
            "markets": [
                {
                    "id": "h_tail",
                    "slug": "paris-july-16-h_tail",
                    "question": "Will the highest temperature in Paris be 29°C or below on July 16?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.008, 0.992],
                },
                {
                    "id": "h31",
                    "slug": "paris-july-16-h31",
                    "question": "Will the highest temperature in Paris be 31°C on July 16?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.25, 0.75],
                },
            ],
        })

        with patch("app.polymarket.markets.get_event_by_slug", return_value=matching_event):
            result = expand_group_children(client, ORIGINAL_JULY_CHILDREN)

        assert len(result) == 2
        assert all("may" not in (m.question or "").lower() for m in result)


class TestGammaDiscoveryFeedsGuardWithRealEventIds:
    def test_stale_may_event_still_rejected_when_children_came_from_keyset_discovery(self):
        """Markets produced by the new /events/keyset discovery path carry real
        parent-event ids into `raw['events']`, so the stale-event expansion
        guard (which trusts those ids) keeps working unchanged."""
        from app.polymarket.discovery_cache import clear_discovery_cache

        clear_discovery_cache()
        client = MagicMock()

        def _get_json(path, params=None):
            if path == "/tags":
                return [{"id": "1", "slug": "sports", "label": "Sports"}]
            if path == "/events/keyset":
                return {"events": [ORIGINAL_EVENT | {"markets": [
                    {
                        "id": "h_tail",
                        "slug": "paris-july-16-h_tail",
                        "question": "Will the highest temperature in Paris be 29°C or below on July 16?",
                        "outcomes": ["Yes", "No"],
                        "outcomePrices": [0.008, 0.992],
                    },
                ]}], "next_cursor": None}
            raise AssertionError(f"unexpected path {path}")

        client.get_json.side_effect = _get_json
        with patch(
            "app.polymarket.discovery_cache.discover_temperature_events",
            return_value=(
                [ORIGINAL_EVENT | {"markets": [
                    {
                        "id": "h_tail",
                        "slug": "paris-july-16-h_tail",
                        "question": "Will the highest temperature in Paris be 29°C or below on July 16?",
                        "outcomes": ["Yes", "No"],
                        "outcomePrices": [0.008, 0.992],
                    },
                ]}],
                MagicMock(),
            ),
        ):
            discovered = _collect_markets_from_gamma_discovery(client)
        assert len(discovered) == 1
        assert discovered[0].raw["events"][0]["id"] == "701548"

        stale_event = _stale_may_event()
        with patch("app.polymarket.markets.get_event_by_slug", return_value=stale_event):
            result = expand_group_children(client, discovered)

        result_questions = [m.question or "" for m in result]
        assert not any("may" in q.lower() for q in result_questions)
        assert result == discovered
