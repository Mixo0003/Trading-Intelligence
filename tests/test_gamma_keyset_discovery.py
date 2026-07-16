"""Tests for worldwide /events/keyset discovery: pagination, tag resolution,
content classification, and closed/inactive/past-date exclusion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.polymarket.gamma_discovery import (
    discover_temperature_events,
    fetch_events_keyset,
    is_daily_temperature_event,
)
from app.polymarket.tags import TagInfo, resolve_weather_tag


def _event(
    event_id: str,
    *,
    title: str = "Highest temperature in Chicago on July 20?",
    slug: str = "highest-temperature-in-chicago-on-july-20-2026",
    description: str = "",
    closed: bool = False,
    active: bool = True,
    end_date: str | None = None,
    tags: list[dict] | None = None,
    markets: list[dict] | None = None,
) -> dict:
    return {
        "id": event_id,
        "title": title,
        "slug": slug,
        "description": description,
        "closed": closed,
        "active": active,
        "endDate": end_date or "2099-01-01T00:00:00Z",
        "tags": tags or [],
        "markets": markets or [],
    }


class TestFetchEventsKeyset:
    def test_follows_every_page_via_next_cursor(self):
        client = MagicMock()
        pages = [
            {"events": [_event("1"), _event("2")], "next_cursor": "cursor-a"},
            {"events": [_event("3")], "next_cursor": "cursor-b"},
            {"events": [_event("4")], "next_cursor": None},
        ]
        client.get_json.side_effect = pages

        events, pages_fetched = fetch_events_keyset(client, closed=False)

        assert pages_fetched == 3
        assert [e["id"] for e in events] == ["1", "2", "3", "4"]

        call_params = [call.kwargs["params"] for call in client.get_json.call_args_list]
        assert "after_cursor" not in call_params[0]
        assert call_params[1]["after_cursor"] == "cursor-a"
        assert call_params[2]["after_cursor"] == "cursor-b"

    def test_stops_on_empty_page(self):
        client = MagicMock()
        client.get_json.side_effect = [
            {"events": [_event("1")], "next_cursor": "cursor-a"},
            {"events": [], "next_cursor": "cursor-b"},
        ]
        events, pages_fetched = fetch_events_keyset(client, closed=False)
        assert pages_fetched == 2
        assert [e["id"] for e in events] == ["1"]

    def test_repeated_cursor_guard_stops_infinite_loop(self):
        client = MagicMock()
        client.get_json.side_effect = [
            {"events": [_event("1")], "next_cursor": "loop-cursor"},
            {"events": [_event("2")], "next_cursor": "loop-cursor"},
            {"events": [_event("3")], "next_cursor": "loop-cursor"},
        ]
        events, pages_fetched = fetch_events_keyset(client, closed=False, max_pages=50)
        # Stops as soon as a cursor repeats, never fetches page 3.
        assert pages_fetched == 2
        assert [e["id"] for e in events] == ["1", "2"]

    def test_hard_cap_is_a_safety_net_not_a_normal_stop(self):
        client = MagicMock()

        def _endless_pages(*_args, **kwargs):
            cursor = kwargs.get("params", {}).get("after_cursor", "0")
            next_cursor = str(int(cursor) + 1) if cursor.isdigit() else "1"
            return {"events": [_event(next_cursor)], "next_cursor": next_cursor}

        client.get_json.side_effect = _endless_pages
        events, pages_fetched = fetch_events_keyset(client, closed=False, max_pages=5)
        assert pages_fetched == 5
        assert len(events) == 5

    def test_never_sends_offset_param(self):
        client = MagicMock()
        client.get_json.side_effect = [{"events": [], "next_cursor": None}]
        fetch_events_keyset(client, closed=False)
        params = client.get_json.call_args.kwargs["params"]
        assert "offset" not in params


class TestResolveWeatherTag:
    def test_exact_slug_match_resolved(self):
        client = MagicMock()
        client.get_json.return_value = [
            {"id": "84", "slug": "earthquakes", "label": "Earthquakes"},
            {"id": "103040", "slug": "weather", "label": "Weather"},
        ]
        tag = resolve_weather_tag(client)
        assert tag == TagInfo(id="103040", slug="weather", label="Weather")

    def test_paginates_with_offset_until_short_page(self):
        client = MagicMock()
        full_page = [{"id": str(i), "slug": f"tag-{i}", "label": f"Tag {i}"} for i in range(100)]
        short_page = [{"id": "999", "slug": "weather", "label": "Weather"}]
        client.get_json.side_effect = [full_page, short_page]
        tag = resolve_weather_tag(client)
        assert tag is not None
        assert tag.id == "999"
        assert client.get_json.call_count == 2

    def test_returns_none_when_tag_absent(self):
        client = MagicMock()
        client.get_json.return_value = [{"id": "1", "slug": "sports", "label": "Sports"}]
        assert resolve_weather_tag(client) is None

    def test_matches_climate_weather_slug_variant(self):
        client = MagicMock()
        client.get_json.return_value = [
            {"id": "1474", "slug": "climate-weather", "label": "climate & weather"},
        ]
        tag = resolve_weather_tag(client)
        assert tag == TagInfo(id="1474", slug="climate-weather", label="climate & weather")

    def test_does_not_false_positive_on_mayweather_tag(self):
        client = MagicMock()
        client.get_json.return_value = [
            {"id": "103891", "slug": "mayweather", "label": "mayweather"},
        ]
        assert resolve_weather_tag(client) is None


class TestIsDailyTemperatureEvent:
    def test_accepts_highest_temperature_title(self):
        event = _event("1", title="Highest temperature in Miami on July 20?")
        assert is_daily_temperature_event(event) is True

    def test_accepts_lowest_temperature_slug_even_without_title_match(self):
        event = _event(
            "1",
            title="Miami weather event",
            slug="lowest-temperature-in-miami-on-july-20-2026",
        )
        assert is_daily_temperature_event(event) is True

    def test_accepts_via_child_market_structure(self):
        event = _event(
            "1",
            title="Miami event",
            slug="miami-event",
            markets=[{"slug": "highest-temperature-in-miami-on-july-20-2026-90f", "question": ""}],
        )
        assert is_daily_temperature_event(event) is True

    def test_rejects_hurricane_event(self):
        event = _event("1", title="Will a hurricane make landfall in Florida this week?")
        assert is_daily_temperature_event(event) is False

    def test_rejects_precipitation_event(self):
        event = _event("1", title="Total precipitation in Seattle this month?")
        assert is_daily_temperature_event(event) is False

    def test_rejects_climate_policy_temperature_increase(self):
        event = _event(
            "1",
            title="Global Temperature Increase (°C) in 2026",
            slug="global-temperature-increase-2026",
        )
        assert is_daily_temperature_event(event) is False

    def test_rejects_unrelated_sports_event(self):
        event = _event(
            "1",
            title="Will the Lakers win the championship?",
            slug="lakers-championship-winner-2026",
        )
        assert is_daily_temperature_event(event) is False


class TestDiscoverTemperatureEvents:
    def _client_with_events(self, tag_id: str, seed_events: list[dict], fallback_events: list[dict]):
        client = MagicMock()

        def _get_json(path, params=None):
            params = params or {}
            if path == "/tags":
                return [{"id": tag_id, "slug": "weather", "label": "Weather"}]
            if path == "/events/keyset":
                if params.get("tag_id") == tag_id:
                    return {"events": seed_events, "next_cursor": None}
                return {"events": fallback_events, "next_cursor": None}
            raise AssertionError(f"unexpected path {path}")

        client.get_json.side_effect = _get_json
        return client

    def test_fallback_finds_untagged_temperature_event(self):
        untagged = _event("55", title="Highest temperature in Denver on July 20?")
        client = self._client_with_events("999", seed_events=[], fallback_events=[untagged])

        events, stats = discover_temperature_events(client)

        assert [e["id"] for e in events] == ["55"]
        assert stats.temperature_parents_retained == 1
        assert stats.tag_id == "999"

    def test_rejects_non_temperature_and_dedupes_by_event_id(self):
        shared = _event("1", title="Highest temperature in Chicago on July 20?")
        hurricane = _event("2", title="Hurricane landfall watch")
        client = self._client_with_events(
            "999",
            seed_events=[shared, hurricane],
            fallback_events=[shared],
        )

        events, stats = discover_temperature_events(client)

        assert [e["id"] for e in events] == ["1"]
        assert stats.active_events_inspected == 2  # shared + hurricane, deduped across passes
        assert stats.temperature_parents_retained == 1

    def test_excludes_closed_inactive_and_past_target_date(self):
        past_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        closed_event = _event("1", closed=True)
        inactive_event = _event("2", active=False)
        past_event = _event("3", end_date=past_date)
        fresh_event = _event("4")
        client = self._client_with_events(
            "999",
            seed_events=[],
            fallback_events=[closed_event, inactive_event, past_event, fresh_event],
        )

        events, stats = discover_temperature_events(client)

        assert [e["id"] for e in events] == ["4"]

    def test_no_tag_resolved_still_runs_fallback(self):
        untagged = _event("7", title="Lowest temperature in Denver on July 20?")
        client = MagicMock()

        def _get_json(path, params=None):
            if path == "/tags":
                return [{"id": "1", "slug": "sports", "label": "Sports"}]
            if path == "/events/keyset":
                return {"events": [untagged], "next_cursor": None}
            raise AssertionError(f"unexpected path {path}")

        client.get_json.side_effect = _get_json
        events, stats = discover_temperature_events(client)
        assert stats.tag_id is None
        assert [e["id"] for e in events] == ["7"]
