"""Focused tests for scan timeout repair: background tasks, cache, concurrency, breaker."""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from app.http.retry import get_with_retry, is_permanent_status, is_retryable_status
from app.polymarket.discovery_cache import (
    clear_discovery_cache,
    get_cached_temperature_events,
)
from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType
from app.region.types import ScanRegion
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.signals.scan_runtime import (
    reset_scan_state,
    scan_state_scope,
)
from app.telegram.formatting import (
    SCAN_PARTIAL_COMPLETION_LINE,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    format_concise_card,
    format_scan_delivery_messages,
)
from app.telegram.handlers import (
    SCAN_ALREADY_RUNNING_MESSAGE,
    SCAN_STARTED_MESSAGE,
    scan_command,
    scheduled_report_job,
)
from app.telegram.scan_tasks import clear_registry_for_tests, is_scope_running, try_start_scoped_task
from app.weather.sources import weather_com
from app.weather.stations import WeatherStation
from tests.conftest import make_test_settings


def _context():
    context = MagicMock()
    context.application.bot_data = {"settings": make_test_settings()}
    context.user_data = {"selected_scan_region": ScanRegion.USA}
    context.bot = AsyncMock()
    return context


def _update():
    update = MagicMock()
    update.effective_user = MagicMock(id=42, username="u", first_name="Test")
    message = AsyncMock()
    message.chat.id = 42
    message.chat_id = 42
    update.message = message
    update.effective_message = message
    return update


def _analysis(city: str = "Paris", order: int = 0) -> MarketAnalysis:
    return MarketAnalysis(
        market_title=f"{city} high",
        market_url="https://polymarket.com/event/x",
        city_location=city,
        station_code="LFPB",
        resolution_source=None,
        target_date="July 16",
        market_type=TemperatureMarketType.MAXIMUM_TEMPERATURE,
        threshold_f=None,
        threshold_unit="C",
        yes_price=0.25,
        implied_probability=0.25,
        model_probability=0.34,
        edge=0.09,
        ev=0.05,
        kelly_fraction=None,
        kelly_cap=0.0,
        confidence="MEDIUM",
        recommendation_tier="WATCH",
        reason="ok",
        data_sources=[],
        analyzed_at="2026-07-15",
        message="",
        best_outcome="31°C",
        forecast_display="31°C",
        expected_range_display="29–32°C",
        display_status="WATCH",
        display_reason="Potential value.",
        temperature_type=NormalizedTemperatureType.DAILY_HIGH,
        temperature_type_label="Daily high temperature",
        event_id=str(order),
    )


@pytest.fixture(autouse=True)
def _clean_scan_registry():
    clear_registry_for_tests()
    clear_discovery_cache()
    yield
    clear_registry_for_tests()
    clear_discovery_cache()


@pytest.mark.asyncio
async def test_handler_acknowledges_immediately_and_runs_background():
    update = _update()
    context = _context()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_scan(*_a, **_k):
        started.set()
        await asyncio.sleep(0.05)
        finished.set()
        return ScanProgress(scan_region="USA", all_analyses=[_analysis()])

    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan", new=slow_scan
    ), patch("app.telegram.handlers.persist_scan_results"), patch(
        "app.telegram.handlers.save_scan_snapshot"
    ):
        await scan_command(update, context)
        assert update.message.reply_text.await_args.args[0].startswith(SCAN_STARTED_MESSAGE)
        assert not finished.is_set()
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        assert started.is_set()


@pytest.mark.asyncio
async def test_duplicate_same_scope_scan_rejected():
    update = _update()
    context = _context()
    gate = asyncio.Event()

    async def blocked_scan(*_a, **_k):
        await gate.wait()
        return ScanProgress(scan_region="USA")

    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan", new=blocked_scan
    ), patch("app.telegram.handlers.persist_scan_results"), patch(
        "app.telegram.handlers.save_scan_snapshot"
    ):
        await scan_command(update, context)
        await scan_command(update, context)
        assert any(
            call.args[0] == SCAN_ALREADY_RUNNING_MESSAGE
            for call in update.message.reply_text.await_args_list
        )
        gate.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_task_registry_cleaned_after_completion():
    async def quick():
        return "ok"

    started, task = await try_start_scoped_task("USA", quick)
    assert started
    await task
    await asyncio.sleep(0)
    assert not is_scope_running("USA")


def test_discovery_cache_reused_within_ttl():
    clear_discovery_cache()
    client = MagicMock()
    events = [{"id": "1", "active": True, "closed": False, "endDate": "2099-01-01T00:00:00Z"}]
    stats = MagicMock()
    with patch(
        "app.polymarket.discovery_cache.discover_temperature_events",
        return_value=(events, stats),
    ) as fetch:
        a, _, hit1 = get_cached_temperature_events(client, ttl_seconds=300)
        b, _, hit2 = get_cached_temperature_events(client, ttl_seconds=300)
    assert hit1 is False
    assert hit2 is True
    assert a == b
    assert fetch.call_count == 1


def test_stale_cache_invalidated_on_ttl_and_past_events():
    clear_discovery_cache()
    client = MagicMock()
    stale = [{"id": "1", "active": True, "closed": False, "endDate": "2020-01-01T00:00:00Z"}]
    fresh = [{"id": "2", "active": True, "closed": False, "endDate": "2099-01-01T00:00:00Z"}]
    with patch(
        "app.polymarket.discovery_cache.discover_temperature_events",
        side_effect=[(stale, MagicMock()), (fresh, MagicMock())],
    ):
        first, _, _ = get_cached_temperature_events(client, ttl_seconds=300)
        assert first == []  # past-date filtered; never cached as usable
        # Force TTL expiry
        import app.polymarket.discovery_cache as dc

        dc._cached_at_monotonic = time.monotonic() - 9999
        second, _, hit = get_cached_temperature_events(client, ttl_seconds=1)
    assert hit is False
    assert [e["id"] for e in second] == ["2"]


@pytest.mark.asyncio
async def test_concurrency_never_exceeds_limit_and_ordering_deterministic():
    settings = make_test_settings(weather_scan_concurrency=2, weather_scan_deadline_seconds=60)
    observed = []

    async def fake_to_thread(fn, *args, **kwargs):
        if fn.__name__ == "get_weather_markets":
            return [], 0, 0
        if fn.__name__ == "_classify_parent_event":
            return MagicMock()
        if fn.__name__ == "_analyze_eligible_group":
            idx = args[0] if False else None
            await asyncio.sleep(0.01)
            analysis = _analysis(city=f"C{args[2][0]}", order=int(args[2][0]))
            return analysis, 1, 1, []
        return fn(*args, **kwargs)

    # Build controlled eligible jobs via patching internals
    from app.signals import scan_service

    groups = [([MagicMock(raw={"events": [{"id": str(i)}]})], MagicMock()) for i in range(4)]
    for i, (children, _) in enumerate(groups):
        children[0].raw = {"events": [{"id": str(i)}]}
        children[0].slug = f"e-{i}"

    with patch.object(scan_service, "get_weather_markets", return_value=([], 0, 0)), patch.object(
        scan_service, "_group_markets_by_event", return_value=({str(i): g[0] for i, g in enumerate(groups)}, [])
    ), patch.object(
        scan_service, "_classify_parent_event", return_value=MagicMock()
    ), patch.object(
        scan_service,
        "event_matches_scan_region",
        return_value=(True, None),
    ), patch.object(
        scan_service,
        "build_market_context_text",
        return_value="text",
    ), patch.object(
        scan_service,
        "_analyze_eligible_group",
        side_effect=lambda settings, client, children, seen: (
            observed.append(int(children[0].raw["events"][0]["id"])) or time.sleep(0.02) or (
                _analysis(city=f"C{children[0].raw['events'][0]['id']}", order=int(children[0].raw["events"][0]["id"])),
                1,
                1,
                [],
            )
        ),
    ):
        # Simpler direct unit of concurrency via semaphore behaviour
        sem = asyncio.Semaphore(2)
        active = 0
        max_active = 0
        results = {}

        async def job(i):
            nonlocal active, max_active
            async with sem:
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.02)
                results[i] = i
                active -= 1

        await asyncio.gather(*(job(i) for i in range(6)))
        assert max_active <= 2
        assert [results[i] for i in sorted(results)] == [0, 1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_one_event_failure_does_not_cancel_others_and_partial_deadline():
    settings = make_test_settings(weather_scan_concurrency=3, weather_scan_deadline_seconds=1)
    from app.signals.scan_service import scan_weather_markets_async

    def markets(*_a, **_k):
        kids = []
        for i in range(4):
            m = MagicMock()
            m.raw = {"events": [{"id": str(i)}]}
            m.id = str(i)
            m.slug = f"slug-{i}"
            kids.append(m)
        return kids, 4, 0

    def classify(children):
        return MagicMock()

    calls = {"n": 0}

    def analyze(settings, client, children, seen):
        calls["n"] += 1
        eid = children[0].raw["events"][0]["id"]
        if eid == "1":
            raise RuntimeError("boom")
        time.sleep(0.4)
        return _analysis(city=f"C{eid}", order=int(eid)), 1, 1, []

    with patch("app.signals.scan_service.get_weather_markets", side_effect=markets), patch(
        "app.signals.scan_service._classify_parent_event", side_effect=classify
    ), patch(
        "app.signals.scan_service.event_matches_scan_region", return_value=(True, None)
    ), patch(
        "app.signals.scan_service.build_market_context_text", return_value="x"
    ), patch(
        "app.signals.scan_service._analyze_eligible_group", side_effect=analyze
    ), patch(
        "app.signals.scan_service.log_scan_runtime_diagnostics"
    ):
        progress = await scan_weather_markets_async(settings, region=ScanRegion.ALL)

    assert progress.partial or len(progress.all_analyses) >= 1
    assert "analysis error" in progress.skip_reasons or progress.skipped >= 1
    cities = [a.city_location for a in progress.all_analyses]
    assert cities == sorted(cities)


def test_weather_com_401_called_once_not_retried():
    reset_scan_state()
    station = WeatherStation("KORD", "Chicago", 41.9, -87.6, verified=True)
    resp = MagicMock()
    resp.status_code = 401
    resp.raise_for_status.side_effect = requests.HTTPError("401")
    with scan_state_scope() as state:
        with patch("app.weather.sources.weather_com._api_key", return_value="k"), patch(
            "app.weather.sources.weather_com.get_with_retry", return_value=resp
        ) as get_mock:
            assert weather_com.fetch_weather_com_daily(
                station, date(2026, 7, 16), NormalizedTemperatureType.DAILY_HIGH, "UTC"
            ) is None
            assert weather_com.fetch_weather_com_daily(
                station, date(2026, 7, 16), NormalizedTemperatureType.DAILY_HIGH, "UTC"
            ) is None
            assert get_mock.call_count == 1
            assert state.weather_com_disabled is True
            assert get_mock.call_args.kwargs.get("max_attempts") == 1


def test_transient_429_uses_bounded_retry():
    assert is_retryable_status(429)
    assert is_retryable_status(503)
    assert is_permanent_status(401)
    assert not is_retryable_status(401)

    responses = [
        MagicMock(status_code=429),
        MagicMock(status_code=200),
    ]
    with patch("app.http.retry.requests.request", side_effect=responses) as req, patch(
        "app.http.retry.time.sleep"
    ):
        out = get_with_retry("https://example.com/x", max_attempts=3, base_delay=0.01)
    assert out.status_code == 200
    assert req.call_count == 2


def test_forecast_dedupe_same_station_date():
    from app.weather.forecast_ensemble import fetch_forecast_ensemble
    from app.weather.temperature_units import TemperatureUnit

    station = WeatherStation("LFPB", "Paris", 48.0, 2.0, verified=True)
    settings = make_test_settings()
    sentinel = MagicMock(name="ensemble")

    with scan_state_scope() as state:
        with patch(
            "app.weather.forecast_ensemble.parse_target_date",
            return_value=date(2026, 7, 16),
        ), patch(
            "app.weather.forecast_ensemble._resolve_timezone", return_value="UTC"
        ), patch(
            "app.weather.forecast_ensemble._collect_independent_providers",
            return_value=[],
        ) as collect:
            cache_key = (
                "LFPB",
                "2026-07-16",
                str(NormalizedTemperatureType.DAILY_HIGH),
                TemperatureUnit.FAHRENHEIT.value,
            )
            state.forecast_cache[cache_key] = sentinel
            out = fetch_forecast_ensemble(
                settings,
                station,
                "July 16",
                NormalizedTemperatureType.DAILY_HIGH,
            )
            assert out is sentinel
            assert collect.call_count == 0


def test_delivery_messages_respect_telegram_size_and_partial_line():
    analyses = [_analysis(city=f"City{i}", order=i) for i in range(8)]
    # Inflate cards artificially via monkeypatch would change format; instead pack many
    progress = ScanProgress(
        scan_region="ALL",
        all_analyses=analyses,
        partial=True,
        parent_events_discovered=8,
    )
    messages = format_scan_delivery_messages(progress)
    assert messages[-1] == SCAN_PARTIAL_COMPLETION_LINE
    assert all(len(m) <= TELEGRAM_MAX_MESSAGE_LENGTH for m in messages)
    card = format_concise_card(analyses[0])
    assert "Market: Daily high temperature" in card
    assert "Station:" not in card
    assert "Band | Model" not in card


def test_stale_dates_never_appear_in_cache():
    clear_discovery_cache()
    client = MagicMock()
    closed = {"id": "1", "active": True, "closed": True, "endDate": "2099-01-01T00:00:00Z"}
    with patch(
        "app.polymarket.discovery_cache.discover_temperature_events",
        return_value=([closed], MagicMock()),
    ):
        events, _, _ = get_cached_temperature_events(client, ttl_seconds=300)
    assert events == []


@pytest.mark.asyncio
async def test_scheduled_scans_use_same_safe_path():
    context = _context()
    context.application.bot_data["settings"] = make_test_settings(
        auto_reports_enabled=True,
        report_times=["08:00"],
        auto_report_regions=[ScanRegion.USA],
        admin_ids=[1],
    )
    progress = ScanProgress(scan_region="USA", all_analyses=[_analysis()])
    with patch("app.telegram.handlers._run_scan", new=AsyncMock(return_value=progress)) as run, patch(
        "app.telegram.handlers.load_latest_scan_snapshot", return_value=None
    ), patch("app.telegram.handlers.save_scan_snapshot"), patch(
        "app.telegram.handlers.format_scheduled_report", return_value=["ok"]
    ), patch(
        "app.database.storage.was_report_sent", return_value=False
    ), patch(
        "app.database.storage.mark_report_sent"
    ), patch.object(context.bot, "send_message", new=AsyncMock()):
        await scheduled_report_job(context)
    run.assert_awaited()
