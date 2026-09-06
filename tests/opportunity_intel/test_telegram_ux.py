"""Checkpoint 6: Market Opportunities Telegram UX + settings + cards."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import opportunity_storage
from app.database.opportunity_storage import (
    initialize_opportunity_schema,
    save_observation_metrics,
    save_scanner_state,
    save_signal,
    save_subscription,
    upsert_market_state,
)
from app.database.storage import initialize_database, upsert_user
from app.opportunity_intel.catalog import (
    TradeCandidatesResult,
    load_top_opportunities,
    load_trade_candidates,
)
from app.copy_intel.order_book import BookLevel, ValidatedOrderBook
from app.opportunity_intel.book import adapt_validated_book
from app.opportunity_intel.discovery import ResultStatus, ValidatedMarket, ValidatedOutcome
from app.opportunity_intel.history import HistoryPoint, HistoryReadResult
from app.opportunity_intel.models import (
    CanonicalObservation,
    ExecutionMetrics,
    LiveMode,
    MovementDirection,
    ObservationMode,
    OpportunityBaseline,
    OpportunityReasonCode,
    OpportunityScannerState,
    OpportunitySignal,
    OpportunitySubscription,
    ScannerStatus,
    SignalFamily,
    SignalStatus,
)
from app.opportunity_intel.payload import build_delivery_card_payload
from app.opportunity_intel.settings import QuietHours, balanced_settings
from app.opportunity_intel.settings_edit import (
    clear_confirmations_for_tests,
    create_edit_confirmation,
    execute_confirmed_settings,
    preview_field_change,
)
from app.opportunity_intel.status_view import build_scanner_status_view
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.inline_menus import (
    CB_COPY,
    CB_OPP,
    CB_OPP_TOP,
    CB_PM_MENU,
    CB_TOP,
    CB_WEATHER,
    main_inline_keyboard,
    opportunity_inline_keyboard,
    polymarket_inline_keyboard,
)
from app.telegram.menu import build_telegram_application
from app.telegram.opportunity_intel.formatting import (
    format_live_mode_help,
    format_opportunity_card,
    format_trade_candidate_card,
    sanitize_plain,
)
from tests.conftest import make_test_settings


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opp_ux.db"),
        admin_ids=[1],
        opportunity_scanner_enabled=True,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    upsert_user(value, 1, "admin", "Admin")
    upsert_user(value, 42, "user", "User")
    clear_confirmations_for_tests()
    return value


def _callback_update(data: str, user_id: int = 42):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = data
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=user_id, username="u", first_name="T")
    update.message = None
    return update


def _context(settings):
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.user_data = {}
    context.bot = MagicMock()
    return context


def _live_gamma_client(
    *,
    now: datetime | None = None,
    market_overrides: dict | None = None,
    event_overrides: dict | None = None,
    side_effect=None,
) -> MagicMock:
    clock = now or datetime.now(timezone.utc)
    market = {
        "id": "market-1",
        "conditionId": "condition-1",
        "question": "Will it rain?",
        "active": True,
        "closed": False,
        "archived": False,
        "resolved": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "live": False,
        "liquidityNum": "100000",
        "volume24hr": "250000",
        "updatedAt": (clock - timedelta(seconds=30)).isoformat(),
        "endDate": (clock + timedelta(days=1)).isoformat(),
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["asset-1", "asset-1-no"],
        "outcomePrices": ["0.55", "0.45"],
        "events": [
            {
                "id": "event-1",
                "slug": "market-one",
                "active": True,
                "closed": False,
                "archived": False,
                "resolved": False,
                "live": False,
                "updatedAt": (clock - timedelta(seconds=30)).isoformat(),
                "endDate": (clock + timedelta(days=1)).isoformat(),
                "tags": [{"id": "politics", "slug": "politics"}],
            }
        ],
    }
    if market_overrides:
        market.update(market_overrides)
    if event_overrides and isinstance(market.get("events"), list) and market["events"]:
        market["events"][0].update(event_overrides)
    client = MagicMock()
    if side_effect is not None:
        client.get_json.side_effect = side_effect
    else:
        client.get_json.return_value = [market]
    return client


def _seed_ready(settings, *, score="80", mode=ObservationMode.NORMAL):
    now = datetime.now(timezone.utc)
    baseline = OpportunityBaseline(
        asset_id="asset-1",
        mode=mode,
        baseline_version=1,
        consecutive_complete_cycles=2,
        last_complete_cycle_id="c2",
        last_observed_at=now,
        ready=True,
    )
    upsert_market_state(
        settings,
        baseline=baseline,
        condition_id="condition-1",
        event_id="event-1",
        market_id="market-1",
        event_slug="market-one",
        question="Will it rain?",
        outcome="Yes",
        category_ids=("politics",),
        metadata_at=now,
        end_at=now + timedelta(days=1),
    )
    opportunity_storage.publish_complete_catalog(
        settings,
        markets=[
            ValidatedMarket(
                event_id="event-1",
                market_id="market-1",
                condition_id="condition-1",
                event_slug="market-one",
                question="Will it rain?",
                category_ids=("politics",),
                mode=mode,
                liquidity_usd=Decimal("100000"),
                volume_24h_usd=Decimal("250000"),
                metadata_at=now,
                end_at=now + timedelta(days=1),
                outcomes=(
                    ValidatedOutcome(
                        asset_id="asset-1",
                        name="Yes",
                        price_hint=Decimal("0.55"),
                        index=0,
                    ),
                    ValidatedOutcome(
                        asset_id="asset-1-no",
                        name="No",
                        price_hint=Decimal("0.45"),
                        index=1,
                    ),
                ),
            )
        ],
        completed_at=now,
        discovery_session_id="sess-top",
        pages_completed=1,
    )
    metrics = tuple(
        ExecutionMetrics(
            asset_id="asset-1",
            observed_at=now,
            allocation_usd=Decimal(a),
            fully_executable=True,
            execution_vwap=Decimal("0.56"),
            slippage=Decimal("0.01"),
            quantity=Decimal("10"),
            consumed_ask_usd=Decimal(a),
        )
        for a in ("25", "50", "100", "250")
    )
    observation = CanonicalObservation(
        asset_id="asset-1",
        condition_id="condition-1",
        event_id="event-1",
        market_id="market-1",
        event_slug="market-one",
        question="Will it rain?",
        outcome="Yes",
        category_ids=("politics",),
        mode=mode,
        observed_at=now,
        metadata_at=now,
        history_at=now,
        book_at=now,
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal("0.02"),
        relative_spread=Decimal("0.03"),
        liquidity_usd=Decimal("100000"),
        volume_24h_usd=Decimal("250000"),
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.05"),
        visible_bid_depth_usd=Decimal("900"),
        visible_ask_depth_usd=Decimal("1000"),
        canonical_execution=next(
            m for m in metrics if m.allocation_usd == Decimal("100")
        ),
    )
    save_observation_metrics(
        settings,
        observation=observation,
        bucket_at=now,
        canonical_score=Decimal(score),
        score_version=1,
        execution_metrics=metrics,
    )
    signal = OpportunitySignal(
        opportunity_uid="opp_ux_1",
        opportunity_key=f"v1:condition-1:asset-1:EXECUTABLE_MOMENTUM:UP:{mode.value}",
        asset_id="asset-1",
        episode_sequence=1,
        material_change_sequence=0,
        family=SignalFamily.EXECUTABLE_MOMENTUM,
        direction=MovementDirection.UP,
        mode=mode,
        score=Decimal(score),
        reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
        signal_at=now,
        first_seen_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=20),
        source_observed_at=now,
        status=SignalStatus.ACTIVE,
    )
    save_signal(settings, signal)
    save_scanner_state(
        settings,
        OpportunityScannerState(
            status=ScannerStatus.HEALTHY,
            schema_version=1,
            baseline_version=1,
            score_version=1,
            global_baseline_cycles=2,
            discovery_complete=True,
            last_complete_discovery_at=now,
            last_complete_cycle_at=now,
            updated_at=now,
        ),
    )
    return signal


@pytest.mark.asyncio
async def test_main_menu_and_namespace_isolation(settings):
    labels = [b.text for row in main_inline_keyboard().inline_keyboard for b in row]
    assert "📈 Polymarket" in labels
    assert "🎯 Market Opportunities" not in labels
    datas = [
        b.callback_data for row in main_inline_keyboard().inline_keyboard for b in row
    ]
    assert CB_PM_MENU in datas
    assert CB_OPP not in datas
    assert CB_TOP not in datas

    pm_datas = [
        b.callback_data
        for row in polymarket_inline_keyboard().inline_keyboard
        for b in row
    ]
    assert CB_OPP in pm_datas
    assert CB_OPP_TOP in pm_datas
    assert CB_WEATHER in pm_datas

    opp_datas = [
        b.callback_data
        for row in opportunity_inline_keyboard().inline_keyboard
        for b in row
    ]
    assert CB_OPP_TOP in opp_datas
    assert CB_PM_MENU in opp_datas
    assert all(not d.startswith("wx:") for d in opp_datas if d)
    assert all(d != CB_TOP for d in opp_datas)

    update = _callback_update(CB_OPP)
    context = _context(settings)
    await inline_callback_handler(update, context)
    update.callback_query.message.edit_text.assert_awaited()
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Market Opportunities" in text


@pytest.mark.asyncio
async def test_weather_and_copy_menus_unchanged(settings):
    update = _callback_update(CB_WEATHER, user_id=1)
    context = _context(settings)
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Weather Intelligence" in text

    update = _callback_update(CB_COPY, user_id=1)
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Copy Trading" in text


def test_opportunities_command_registered(settings):
    app = build_telegram_application(settings)
    names = []
    for handlers in app.handlers.values():
        for handler in handlers:
            callback = getattr(handler, "callback", None)
            if callback is not None:
                names.append(getattr(callback, "__name__", ""))
    assert "opportunities_command" in names


def test_top_opportunities_catalog_filters_and_limit(settings):
    _seed_ready(settings)
    save_subscription(
        settings,
        OpportunitySubscription(
            telegram_id=42,
            enabled=True,
            settings=balanced_settings(),
        ),
        expected_revision=None,
    )
    result = load_top_opportunities(
        settings,
        telegram_id=42,
        gamma_client=_live_gamma_client(),
    )
    assert result.status == "ready"
    assert 1 <= len(result.entries) <= 3
    assert result.entries[0].payload.allocation_usd == Decimal("100")


def test_v229_trade_candidates_refresh_history_book_and_render_wait(settings):
    _seed_ready(settings)
    now = datetime.now(timezone.utc)
    gamma = _live_gamma_client(
        now=now,
        market_overrides={
            "description": "This market resolves from the official published result.",
            "resolutionSource": "https://official.example/result",
            "feesEnabled": True,
            "feeSchedule": {
                "rate": "0.04",
                "exponent": 1,
                "takerOnly": True,
            },
        },
    )
    history = MagicMock()
    history.fetch.return_value = HistoryReadResult(
        asset_id="asset-1",
        status=ResultStatus.COMPLETE,
        points=(
            HistoryPoint(at=now - timedelta(hours=1), price=Decimal("0.50")),
            HistoryPoint(at=now - timedelta(minutes=15), price=Decimal("0.52")),
            HistoryPoint(at=now - timedelta(minutes=5), price=Decimal("0.54")),
            HistoryPoint(at=now, price=Decimal("0.56")),
        ),
    )
    book = ValidatedOrderBook(
        asset_id="asset-1",
        book_hash="fresh",
        quote_at=now.timestamp(),
        received_at=now.timestamp(),
        quote_age_seconds=0,
        bids=(BookLevel(price=0.54, size=1000),),
        asks=(BookLevel(price=0.56, size=1000),),
        best_bid=0.54,
        best_ask=0.56,
        midpoint=0.55,
        spread=0.02,
        visible_ask_depth_usd=560,
        visible_bid_depth_usd=540,
        min_order_size=5,
    )
    book_client = MagicMock()
    book_client.fetch.return_value = adapt_validated_book(
        book,
        requested_asset="asset-1",
        mode=ObservationMode.NORMAL,
    )
    result = load_trade_candidates(
        settings,
        telegram_id=42,
        now=now,
        gamma_client=gamma,
        history_client=history,
        book_client=book_client,
    )
    assert result.status == "ready"
    assert result.entries[0].decision.action.value == "WAIT"
    text = format_trade_candidate_card(
        result.entries[0].payload, result.entries[0].decision
    )
    assert text.startswith("POLYMARKET TRADE CANDIDATE")
    assert "Рішення: WAIT" in text


def test_top_opportunities_status_messages(settings):
    empty = load_top_opportunities(
        settings, telegram_id=42, gamma_client=_live_gamma_client()
    )
    assert empty.status == "discovering"
    now = datetime.now(timezone.utc)
    save_scanner_state(
        settings,
        OpportunityScannerState(
            status=ScannerStatus.DISABLED,
            schema_version=1,
            baseline_version=1,
            score_version=1,
            global_baseline_cycles=0,
            discovery_complete=False,
            updated_at=now,
        ),
    )
    disabled = load_top_opportunities(
        settings, telegram_id=42, gamma_client=_live_gamma_client()
    )
    assert disabled.status == "disabled"


def test_card_rendering_normal_and_strict():
    now = datetime.now(timezone.utc)
    payload = build_delivery_card_payload(
        opportunity_uid="opp_card",
        signal_at=now,
        wave_at=now,
        signal_family=SignalFamily.EXECUTABLE_MOMENTUM,
        question="Will <b>it</b> rain? *yes*",
        outcome="Yes_outcome",
        event_slug="market-one",
        mode=ObservationMode.NORMAL,
        canonical_score=Decimal("81.5"),
        reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
        direction=MovementDirection.UP,
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.05"),
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal("0.02"),
        relative_spread=Decimal("0.03"),
        allocation_usd=Decimal("250"),
        execution_vwap=Decimal("0.56"),
        slippage=Decimal("0.01"),
        executable_ask_depth_usd=Decimal("1000"),
        volume_24h_usd=Decimal("250000"),
        liquidity_usd=Decimal("100000"),
        end_at=now + timedelta(hours=8),
        source_observed_at=now,
    )
    text = format_opportunity_card(payload, now=now)
    assert text.startswith("📈 MOMENTUM СИГНАЛ")
    assert "Will <b>it</b> rain? *yes*" in text
    assert "📊 Score: 81.5" in text
    assert "✅ швидкий рух за 5 хвилин" in text
    assert "💵 Вхід: 0.5600" in text
    assert "💰 $250.00:" in text
    assert "🔗 Відкрити на Polymarket" in text
    assert "https://polymarket.com/event/market-one" in text
    assert "opp_card" not in text
    assert "source_observed_at" not in text
    assert "wave_at" not in text
    assert "FAST_5M_MOVE" not in text
    assert "{" not in text and "}" not in text
    body = text.split("Will <b>it</b> rain? *yes*", 1)[1]
    assert "<b>" not in body
    assert "Trade now" not in text
    assert "place a trade" not in text.lower()
    assert "guaranteed" not in text.lower()
    assert len(text) <= 4096

    live = build_delivery_card_payload(
        opportunity_uid="opp_live",
        signal_at=now,
        wave_at=now,
        signal_family=SignalFamily.EXECUTABLE_MOMENTUM,
        question="Live game?",
        outcome="Yes",
        event_slug="market-one",
        mode=ObservationMode.LIVE_STRICT,
        canonical_score=Decimal("88"),
        reason_codes=(OpportunityReasonCode.LIVE_MARKET,),
        direction=MovementDirection.UP,
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.05"),
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal("0.02"),
        relative_spread=Decimal("0.03"),
        allocation_usd=Decimal("100"),
        execution_vwap=Decimal("0.56"),
        slippage=Decimal("0.01"),
        executable_ask_depth_usd=Decimal("1000"),
        volume_24h_usd=Decimal("250000"),
        liquidity_usd=Decimal("100000"),
        end_at=now + timedelta(hours=2),
        source_observed_at=now,
    )
    live_text = format_opportunity_card(live, now=now)
    assert "Live-ринок" in live_text
    assert "EXTRA SAFETY CHECKS" not in live_text
    assert "STRICT" not in live_text
    help_text = format_live_mode_help(live_mode=LiveMode.OFF)
    assert help_text == "Live Mode\nCurrent status: OFF"
    assert "strict" not in help_text.lower()


def test_settings_preview_confirm_and_stolen_token(settings):
    clear_confirmations_for_tests()
    current = balanced_settings()
    proposed, lines = preview_field_change(
        current, field_key="minimum_score", raw_value="70"
    )
    conf = create_edit_confirmation(
        settings,
        operator_id=42,
        starting=current,
        proposed=proposed,
        changed_keys=["minimum_score"],
        preview_lines=lines,
    )
    status, sub = execute_confirmed_settings(
        settings, token=conf.token, operator_id=99
    )
    assert status == "operator_mismatch"
    status, sub = execute_confirmed_settings(
        settings, token=conf.token, operator_id=42
    )
    assert status == "ok"
    assert sub is not None
    assert sub.settings.minimum_score == 70


def test_settings_quiet_hours_overnight_and_reset_bounds(settings):
    current = balanced_settings()
    proposed, _ = preview_field_change(
        current, field_key="quiet_hours", raw_value="22:00-06:00"
    )
    assert proposed.quiet_hours == QuietHours(start=time(22, 0), end=time(6, 0))
    with pytest.raises(ValueError):
        preview_field_change(current, field_key="minimum_score", raw_value="10")


def test_scanner_status_and_history_safe(settings):
    _seed_ready(settings)
    view = build_scanner_status_view(settings)
    assert view.label == "Ready"
    assert "database" not in view.text.lower()
    assert "pid" not in view.text.lower()
    assert ".env" not in view.text


@pytest.mark.asyncio
async def test_top_callback_uses_cached_path(settings):
    _seed_ready(settings)
    update = _callback_update(CB_OPP_TOP)
    context = _context(settings)
    client = _live_gamma_client()
    with (
        patch(
            "app.opportunity_intel.catalog.PolymarketClient",
            return_value=client,
        ),
        patch(
            "app.telegram.opportunity_intel.handlers.load_trade_candidates",
            return_value=TradeCandidatesResult(
                status="empty",
                entries=(),
                message="No verified trade candidates.",
            ),
        ) as mocked,
    ):
        await inline_callback_handler(update, context)
        mocked.assert_called()
    import app.telegram.opportunity_intel.handlers as handlers_mod

    source = open(handlers_mod.__file__, encoding="utf-8").read()
    assert "gamma" not in source.lower()
    assert "fetch_validated_order_book" not in source
    assert "run_cycle" not in source
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert text == "No verified trade candidates."
    assert "opportunity_uid" not in text
    assert not text.lstrip().startswith("{")


def test_sanitize_plain_strips_markup():
    assert "<" not in sanitize_plain("a <script> b")
    assert "*" not in sanitize_plain("a *b* c")
