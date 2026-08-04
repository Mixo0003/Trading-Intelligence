"""V2.2.3 Opportunity UX fixes: prompts, menu separation, states, baseline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.copy_intel.api_result import ApiSuccess
from app.copy_intel.order_book import parse_validated_order_book
from app.database.opportunity_storage import (
    SqliteDiscoverySessionStore,
    count_baseline_ready_assets,
    initialize_opportunity_schema,
    load_all_market_states,
    load_discovery_progress,
    load_scanner_state,
    load_subscription,
    save_scanner_state,
)
from app.database.storage import initialize_database, upsert_user
from app.opportunity_intel.catalog import (
    BASELINING_MESSAGE,
    DISCOVERING_MESSAGE,
    READY_EMPTY_MESSAGE,
    load_top_opportunities,
)
from app.opportunity_intel.discovery import CompleteDiscoveryCache, ResultStatus
from app.opportunity_intel.discovery_session import (
    MAX_SESSION_DIAGNOSTICS,
    advance_discovery_session,
)
from app.opportunity_intel.history import (
    FreshnessStatus,
    HistoryDirection,
    HistoryPoint,
    HistoryReadResult,
    MovementResult,
)
from app.opportunity_intel.models import OpportunityScannerState, ScannerStatus
from app.opportunity_intel.runtime import get_discovery_cache, reset_discovery_cache
from app.opportunity_intel.scanner import OpportunityScannerService
from app.opportunity_intel.settings_edit import (
    ALERT_FIELD_KEYS,
    FILTER_FIELD_KEYS,
    clear_confirmations_for_tests,
    field_prompt,
)
from app.opportunity_intel.status_view import build_scanner_status_view
from app.telegram.inline_menus import (
    CB_OPP_ALERTS,
    CB_OPP_FILTERS,
    CB_OPP_LIVE,
    CB_OSET_CANCEL,
    CB_OSET_CONFIRM,
    opportunity_filters_keyboard,
    opportunity_inline_keyboard,
    opportunity_settings_keyboard,
)
from app.telegram.opportunity_intel.formatting import format_live_mode_help
from app.telegram.opportunity_intel.handlers import (
    handle_opportunity_callback,
    opportunity_settings_text_input,
)
from tests.conftest import make_test_settings
from tests.opportunity_intel.test_discovery import NOW, _client, _event, _market

USER_ID = 42


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opp_ux_fixes.db"),
        admin_ids=[1],
        opportunity_scanner_enabled=True,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    upsert_user(value, USER_ID, "user", "User")
    clear_confirmations_for_tests()
    reset_discovery_cache()
    yield value
    reset_discovery_cache()
    clear_confirmations_for_tests()


def _callback_update():
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=USER_ID, username="u", first_name="T")
    update.message = None
    return update


def _text_update(text: str):
    update = _callback_update()
    update.callback_query = None
    message = AsyncMock()
    message.text = text
    update.effective_message = message
    update.message = message
    return update


def _context(settings):
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.user_data = {}
    context.bot = MagicMock()
    return context


async def _emit(settings, context, data: str) -> tuple[str, object]:
    update = _callback_update()
    await handle_opportunity_callback(update, context, data)
    return _last_message(update)


def _last_message(update) -> tuple[str, object]:
    message = update.effective_message
    call = message.edit_text.await_args or message.reply_text.await_args
    assert call is not None
    return call.args[0], call.kwargs.get("reply_markup")


def _callback_data(keyboard) -> set[str]:
    return {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    }


# --- Step 3: per-field prompt formats -------------------------------------


def test_numeric_prompts_never_show_quiet_hours_format():
    numeric_fields = (
        "minimum_score",
        "minimum_liquidity_usd",
        "minimum_24h_volume_usd",
        "maximum_relative_spread",
        "maximum_absolute_spread",
        "maximum_slippage",
        "paper_allocation_usd",
        "cooldown_seconds",
    )
    for key in numeric_fields:
        prompt = field_prompt(key)
        assert "HH:MM" not in prompt, key
        assert "quiet" not in prompt.lower(), key
        assert "Example:" in prompt, key


def test_each_prompt_states_only_its_own_format():
    assert "whole number" in field_prompt("minimum_score")
    assert "Example: 65" in field_prompt("minimum_score")
    assert "USD amount" in field_prompt("minimum_liquidity_usd")
    assert "Example: 10000" in field_prompt("minimum_liquidity_usd")
    assert "Example: 25000" in field_prompt("minimum_24h_volume_usd")
    assert "probability-point" in field_prompt("maximum_absolute_spread")
    assert "Example: 0.05" in field_prompt("maximum_absolute_spread")
    assert "seconds" in field_prompt("cooldown_seconds")
    assert "Example: 1800" in field_prompt("cooldown_seconds")
    assert "timezone" in field_prompt("timezone_name").lower()
    assert "America/Chicago" in field_prompt("timezone_name")


def test_quiet_hours_prompt_is_the_only_time_range_prompt():
    prompt = field_prompt("quiet_hours")
    assert "HH:MM-HH:MM or off" in prompt
    assert "Example: 23:00-07:00" in prompt


def test_prompt_examples_are_accepted_by_unchanged_validation():
    from app.opportunity_intel.settings import balanced_settings
    from app.opportunity_intel.settings_edit import (
        FIELD_INPUT_FORMATS,
        apply_field_value,
    )

    current = balanced_settings()
    for key in ("minimum_score", "minimum_liquidity_usd", "minimum_24h_volume_usd",
                "maximum_relative_spread", "maximum_absolute_spread",
                "maximum_slippage", "paper_allocation_usd", "cooldown_seconds",
                "quiet_hours"):
        example = FIELD_INPUT_FORMATS[key][1]
        apply_field_value(current, field_key=key, raw_value=example)


# --- Step 4: Filters vs Alert Settings ------------------------------------


def test_filters_and_alert_keyboards_expose_different_fields():
    filters = _callback_data(opportunity_filters_keyboard())
    alerts = _callback_data(opportunity_settings_keyboard())
    assert {"oset:f:ms", "oset:f:ml", "oset:f:mv", "oset:f:rs", "oset:f:as",
            "oset:f:sl", "oset:cat", "oset:lm"} <= filters
    assert {"oset:am", "oset:f:cd", "oset:f:qh", "oset:f:tz", "oset:f:pa"} <= alerts
    assert not (filters & alerts) - {"nav:opp"}
    assert "oset:f:qh" not in filters
    assert "oset:f:ms" not in alerts


def test_field_groups_are_disjoint_and_complete():
    assert not set(FILTER_FIELD_KEYS) & set(ALERT_FIELD_KEYS)
    assert "quiet_hours" in ALERT_FIELD_KEYS
    assert "minimum_score" in FILTER_FIELD_KEYS


@pytest.mark.asyncio
async def test_filters_callback_shows_only_filter_fields(settings):
    context = _context(settings)
    text, keyboard = await _emit(settings, context, CB_OPP_FILTERS)
    assert text.startswith("Opportunity Filters")
    assert "Minimum score" in text
    assert "Live mode" in text
    assert "Quiet hours" not in text
    assert "Alert mode" not in text
    assert "oset:f:ms" in _callback_data(keyboard)


@pytest.mark.asyncio
async def test_alert_settings_callback_shows_only_alert_fields(settings):
    context = _context(settings)
    text, keyboard = await _emit(settings, context, CB_OPP_ALERTS)
    assert text.startswith("Opportunity Alert Settings")
    assert "Alert mode" in text
    assert "Quiet hours" in text
    assert "Global user delivery" in text
    assert "Minimum score" not in text
    assert "Maximum slippage" not in text
    assert "oset:f:qh" in _callback_data(keyboard)


@pytest.mark.asyncio
async def test_min_liquidity_prompt_is_numeric_and_stays_in_filters(settings):
    context = _context(settings)
    text, keyboard = await _emit(settings, context, "oset:f:ml")
    assert "USD amount" in text
    assert "HH:MM" not in text
    assert "oset:f:ms" in _callback_data(keyboard)
    assert "oset:f:qh" not in _callback_data(keyboard)


@pytest.mark.asyncio
async def test_quiet_hours_prompt_stays_in_alert_settings(settings):
    context = _context(settings)
    text, keyboard = await _emit(settings, context, "oset:f:qh")
    assert "HH:MM-HH:MM or off" in text
    assert "oset:f:cd" in _callback_data(keyboard)
    assert "oset:f:ms" not in _callback_data(keyboard)


@pytest.mark.asyncio
async def test_quiet_hours_edit_preview_confirm_returns_to_alert_settings(settings):
    context = _context(settings)
    await _emit(settings, context, "oset:f:qh")
    update = _text_update("23:00-07:00")
    assert await opportunity_settings_text_input(update, context) is True
    preview, _markup = _last_message(update)
    assert "Confirm Opportunity Settings change?" in preview
    assert "Quiet hours" in preview
    assert context.user_data["opp_settings_confirm_token"]

    text, keyboard = await _emit(settings, context, CB_OSET_CONFIRM)
    assert text.startswith("Opportunity Alert Settings")
    assert "23:00-07:00" in text
    sub = load_subscription(settings, USER_ID)
    assert sub is not None
    assert sub.settings.quiet_hours is not None
    assert "oset:f:qh" in _callback_data(keyboard)


@pytest.mark.asyncio
async def test_filter_edit_cancel_returns_to_filters_and_keeps_value(settings):
    context = _context(settings)
    await _emit(settings, context, "oset:f:ms")
    update = _text_update("70")
    assert await opportunity_settings_text_input(update, context) is True
    text, keyboard = await _emit(settings, context, CB_OSET_CANCEL)
    assert text.startswith("Opportunity Filters")
    assert "Minimum score: 65" in text
    assert load_subscription(settings, USER_ID) is None
    assert "oset:f:ms" in _callback_data(keyboard)


@pytest.mark.asyncio
async def test_stale_preview_protection_survives_menu_split(settings):
    context = _context(settings)
    await _emit(settings, context, "oset:f:ms")
    stale_update = _text_update("70")
    await opportunity_settings_text_input(stale_update, context)
    stale_token = context.user_data["opp_settings_confirm_token"]

    other = _context(settings)
    await _emit(settings, other, "oset:f:ms")
    fresh_update = _text_update("75")
    await opportunity_settings_text_input(fresh_update, other)
    await _emit(settings, other, CB_OSET_CONFIRM)
    assert load_subscription(settings, USER_ID).settings.minimum_score == 75

    context.user_data["opp_settings_confirm_token"] = stale_token
    text, _keyboard = await _emit(settings, context, CB_OSET_CONFIRM)
    assert "Could not save settings" in text
    assert load_subscription(settings, USER_ID).settings.minimum_score == 75


# --- Step 5: Live Mode ----------------------------------------------------


def test_live_mode_button_replaces_live_markets_label():
    labels = [
        button.text
        for row in opportunity_inline_keyboard().inline_keyboard
        for button in row
    ]
    assert "Live Mode" in labels
    assert "Live Markets" not in labels


@pytest.mark.asyncio
async def test_live_mode_screen_explains_filtering_not_a_market_list(settings):
    context = _context(settings)
    text, _keyboard = await _emit(settings, context, CB_OPP_LIVE)
    assert text.startswith("Live Mode")
    lowered = text.lower()
    assert "off excludes live / in-play markets" in lowered
    assert "stricter checks" in lowered
    assert "configures filtering" in lowered
    assert "not a list of active markets" in lowered
    assert "no trades are placed" in lowered
    assert format_live_mode_help() == text


# --- Step 6: Top Opportunities and Scanner Status states ------------------


def _state(
    *,
    status: ScannerStatus,
    discovery_complete: bool,
    cycles: int,
    last: datetime,
) -> OpportunityScannerState:
    return OpportunityScannerState(
        status=status,
        schema_version=1,
        baseline_version=1,
        score_version=1,
        global_baseline_cycles=cycles,
        discovery_complete=discovery_complete,
        last_complete_discovery_at=last if discovery_complete else None,
        last_complete_cycle_at=last if discovery_complete else None,
        updated_at=last,
    )


def test_top_opportunities_reports_discovery_in_progress(settings):
    result = load_top_opportunities(settings, telegram_id=USER_ID)
    assert result.status == "discovering"
    assert result.message == DISCOVERING_MESSAGE

    save_scanner_state(
        settings,
        _state(
            status=ScannerStatus.BASELINING,
            discovery_complete=False,
            cycles=0,
            last=datetime.now(timezone.utc),
        ),
    )
    result = load_top_opportunities(settings, telegram_id=USER_ID)
    assert result.status == "discovering"
    assert result.message == DISCOVERING_MESSAGE


def test_top_opportunities_reports_baseline_in_progress(settings):
    save_scanner_state(
        settings,
        _state(
            status=ScannerStatus.BASELINING,
            discovery_complete=True,
            cycles=1,
            last=datetime.now(timezone.utc),
        ),
    )
    result = load_top_opportunities(settings, telegram_id=USER_ID)
    assert result.status == "baselining"
    assert result.message == BASELINING_MESSAGE


def test_top_opportunities_reports_ready_with_zero_results(settings):
    from tests.opportunity_intel.test_telegram_ux import _live_gamma_client, _seed_ready

    _seed_ready(settings, score="55")
    result = load_top_opportunities(
        settings, telegram_id=USER_ID, gamma_client=_live_gamma_client()
    )
    assert result.status == "empty"
    assert result.message == READY_EMPTY_MESSAGE


def test_top_opportunities_reports_ready_with_results(settings):
    from tests.opportunity_intel.test_telegram_ux import _live_gamma_client, _seed_ready

    _seed_ready(settings, score="85")
    result = load_top_opportunities(
        settings, telegram_id=USER_ID, gamma_client=_live_gamma_client()
    )
    assert result.status == "ready"
    assert result.entries


def test_scanner_status_separates_discovery_baseline_and_delivery(settings):
    save_scanner_state(
        settings,
        _state(
            status=ScannerStatus.BASELINING,
            discovery_complete=True,
            cycles=1,
            last=datetime.now(timezone.utc),
        ),
    )
    view = build_scanner_status_view(settings)
    assert view.scanner_enabled is True
    assert view.label == "Baselining"
    assert view.baseline_ready_count == 0
    assert "Scanner: enabled" in view.text
    assert "Discovery: complete" in view.text
    assert "Baseline-ready markets: 0" in view.text
    assert "Qualifying opportunities: 0" in view.text
    assert "Automatic alerts: off" in view.text
    assert "Automatic user delivery: not available globally" in view.text
    assert "State: Baselining" in view.text
    assert "Baseline: In progress" in view.text


def test_scanner_status_never_reports_ready_without_baseline_evidence(settings):
    save_scanner_state(
        settings,
        _state(
            status=ScannerStatus.HEALTHY,
            discovery_complete=True,
            cycles=2,
            last=datetime.now(timezone.utc),
        ),
    )
    view = build_scanner_status_view(settings)
    assert view.baseline_ready_count == 0
    assert view.label == "Baselining"

    from tests.opportunity_intel.test_telegram_ux import _seed_ready

    _seed_ready(settings)
    ready = build_scanner_status_view(settings)
    assert ready.baseline_ready_count == 1
    assert ready.label == "Ready"


def test_scanner_status_shows_live_progress_age_while_discovery_advances(settings):
    store = SqliteDiscoverySessionStore(settings)
    cache = CompleteDiscoveryCache(
        ttl_seconds=300, session_ttl_seconds=3600, session_store=store
    )
    client = _client({"events": [_event("e1")], "next_cursor": "cursor-a"})
    progressing = cache.advance_from_client(client, now=NOW, max_attempts=1)
    assert progressing.status == ResultStatus.IN_PROGRESS

    save_scanner_state(
        settings,
        _state(
            status=ScannerStatus.BASELINING,
            discovery_complete=True,
            cycles=1,
            last=NOW - timedelta(hours=3),
        ),
    )
    view = build_scanner_status_view(settings, now=NOW + timedelta(seconds=30))
    assert view.discovery_label == "in progress"
    assert view.progress_age_seconds == 30
    assert view.cache_age_seconds is not None and view.cache_age_seconds > 3600
    assert "Discovery pages completed: 1" in view.text
    assert "Discovery progress age: 30s" in view.text


# --- Step 7: verified runtime defects ------------------------------------


def _book(asset_id: str, *, timestamp: float = 1_000.0):
    payload = {
        "asset_id": asset_id,
        "timestamp": timestamp,
        "hash": f"hash-{asset_id}",
        "bids": [{"price": "0.495", "size": "5000"}],
        "asks": [{"price": "0.505", "size": "5000"}],
    }
    result = parse_validated_order_book(
        payload, requested_asset=asset_id, received_at=timestamp + 5
    )
    assert isinstance(result, ApiSuccess)
    return result.data


class _Books:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, asset_id, *, mode, received_at=None):
        from app.opportunity_intel.book import adapt_validated_book

        self.calls += 1
        return adapt_validated_book(
            _book(asset_id), requested_asset=asset_id, mode=mode
        )


class _History:
    def fetch(self, asset_id, *, start_at, end_at):
        return HistoryReadResult(
            asset_id=asset_id,
            status=ResultStatus.COMPLETE,
            points=(
                HistoryPoint(at=NOW - timedelta(hours=1), price=Decimal("0.50")),
                HistoryPoint(at=NOW - timedelta(seconds=10), price=Decimal("0.50")),
            ),
        )


def _calm_movement(asset_id: str) -> MovementResult:
    """Complete, fresh history with movement below every trigger."""
    return MovementResult(
        asset_id=asset_id,
        status=ResultStatus.COMPLETE,
        freshness=FreshnessStatus.FRESH,
        diagnostic_code=None,
        latest_price=Decimal("0.50"),
        latest_at=NOW - timedelta(seconds=10),
        price_5m=Decimal("0.4995"),
        price_15m=Decimal("0.499"),
        price_1h=Decimal("0.498"),
        change_5m_pp=Decimal("0.05"),
        change_15m_pp=Decimal("0.1"),
        change_1h_pp=Decimal("0.2"),
        absolute_change_5m_pp=Decimal("0.05"),
        absolute_change_15m_pp=Decimal("0.1"),
        absolute_change_1h_pp=Decimal("0.2"),
        direction=HistoryDirection.UP,
    )


def _patch_calm_movement(monkeypatch):
    monkeypatch.setattr(
        "app.opportunity_intel.scanner.select_movement",
        lambda history, *, mode, now: _calm_movement(history.asset_id),
    )


def test_process_discovery_cache_is_reused_across_cycles(settings):
    first = get_discovery_cache(settings)
    assert get_discovery_cache(settings) is first
    reset_discovery_cache()
    assert get_discovery_cache(settings) is not first


def test_complete_universe_survives_next_cycle_and_baseline_advances(
    settings, monkeypatch
):
    _patch_calm_movement(monkeypatch)
    client = _client(
        {
            "events": [_event("e1", markets=[_market("m1")])],
            "next_cursor": None,
        }
    )
    cache = get_discovery_cache(settings)
    service = OpportunityScannerService(
        settings,
        discovery_cache=cache,
        gamma_client=client,
        history_client=_History(),
        book_client=_Books(),
        concurrency=1,
    )

    cycle1 = service.run_cycle(now=NOW, strict_live_demand=False)
    assert cycle1.discovery_status == ResultStatus.COMPLETE
    assert cycle1.outcomes_shortlisted == 2
    assert cycle1.observations_eligible == 0
    assert cycle1.baseline_advanced == 2
    assert cycle1.signals_created == 0
    assert load_discovery_progress(settings) is None
    states = load_all_market_states(settings)
    assert states
    assert all(s.baseline.consecutive_complete_cycles == 1 for s in states)
    assert all(s.baseline.ready is False for s in states)

    # Second cycle: trusted universe is still cached, so no new Gamma page.
    gamma_calls_after_first = client.get_json.call_count
    cycle2 = service.run_cycle(
        now=NOW + timedelta(seconds=120), strict_live_demand=False
    )
    assert client.get_json.call_count == gamma_calls_after_first
    assert cycle2.discovery_status == ResultStatus.COMPLETE
    assert cycle2.baseline_advanced == 2
    assert cycle2.signals_created == 0
    states = load_all_market_states(settings)
    assert all(s.baseline.consecutive_complete_cycles == 2 for s in states)
    assert all(s.baseline.ready is True for s in states)
    assert all(s.was_eligible_at_baseline is False for s in states)
    assert count_baseline_ready_assets(settings) == 2

    state = load_scanner_state(settings)
    assert state is not None
    assert state.discovery_complete is True
    assert state.global_baseline_cycles == 2
    assert state.status == ScannerStatus.HEALTHY


def test_baseline_advances_on_complete_but_below_floor_observation(
    settings, monkeypatch
):
    _patch_calm_movement(monkeypatch)
    client = _client(
        {"events": [_event("e1", markets=[_market("m1")])], "next_cursor": None}
    )
    service = OpportunityScannerService(
        settings,
        discovery_cache=get_discovery_cache(settings),
        gamma_client=client,
        history_client=_History(),
        book_client=_Books(),
        concurrency=1,
    )
    cycle = service.run_cycle(now=NOW, strict_live_demand=False)
    assert cycle.histories_valid == 2
    assert cycle.books_valid == 2
    assert cycle.observations_eligible == 0
    assert cycle.observations_rejected == 2
    assert cycle.baseline_advanced == 2


def test_incomplete_observation_never_advances_baseline(settings, monkeypatch):
    """Identity/freshness failures are rejections too, but prove nothing."""
    monkeypatch.setattr(
        "app.opportunity_intel.scanner.select_movement",
        lambda history, *, mode, now: _calm_movement("other-asset"),
    )
    client = _client(
        {"events": [_event("e1", markets=[_market("m1")])], "next_cursor": None}
    )
    service = OpportunityScannerService(
        settings,
        discovery_cache=get_discovery_cache(settings),
        gamma_client=client,
        history_client=_History(),
        book_client=_Books(),
        concurrency=1,
    )
    cycle = service.run_cycle(now=NOW, strict_live_demand=False)
    assert cycle.observations_eligible == 0
    assert cycle.baseline_advanced == 0
    assert count_baseline_ready_assets(settings) == 0


def test_session_diagnostics_are_bounded(settings):
    events = [
        _event(f"e{index}", active=False)
        for index in range(MAX_SESSION_DIAGNOSTICS + 50)
    ]
    client = _client({"events": events, "next_cursor": None})
    advanced = advance_discovery_session(client, None, now=NOW, max_attempts=1)
    assert advanced.result.status == ResultStatus.COMPLETE
    assert len(advanced.result.diagnostics) <= MAX_SESSION_DIAGNOSTICS


def test_no_delivery_side_effects_from_ux_and_scan(settings, monkeypatch):
    from app.database import opportunity_storage

    _patch_calm_movement(monkeypatch)
    client = _client(
        {"events": [_event("e1", markets=[_market("m1")])], "next_cursor": None}
    )
    service = OpportunityScannerService(
        settings,
        discovery_cache=get_discovery_cache(settings),
        gamma_client=client,
        history_client=_History(),
        book_client=_Books(),
        concurrency=1,
    )
    service.run_cycle(now=NOW, strict_live_demand=False)
    service.run_cycle(now=NOW + timedelta(seconds=120), strict_live_demand=False)

    with opportunity_storage._opportunity_connection(settings) as connection:
        deliveries = connection.execute(
            "SELECT COUNT(*) AS n FROM opportunity_deliveries"
        ).fetchone()["n"]
        signals = connection.execute(
            "SELECT COUNT(*) AS n FROM opportunity_signals"
        ).fetchone()["n"]
        sent = connection.execute(
            "SELECT COUNT(*) AS n FROM opportunity_deliveries WHERE status = 'SENT'"
        ).fetchone()["n"]
    assert deliveries == 0
    assert signals == 0
    assert sent == 0
    gates = opportunity_storage.load_delivery_gates(settings)
    assert gates.user_delivery_enabled is False
