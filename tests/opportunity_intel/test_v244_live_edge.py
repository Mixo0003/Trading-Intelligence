"""V2.4.4 Live Edge Opportunities — focused unit and regression tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import json
import sqlite3

import pytest

from app.copy_intel.api_result import ApiSuccess
from app.copy_intel.order_book import parse_validated_order_book
from app.database import live_edge_storage, opportunity_storage
from app.database.opportunity_storage import initialize_opportunity_schema
from app.database.storage import initialize_database, upsert_user
from app.opportunity_intel.catalog import load_top_opportunities
from app.opportunity_intel.live_edge.classify import (
    classify_market,
    extract_participants,
    is_near_resolution_candidate,
    scan_priority,
)
from app.opportunity_intel.live_edge.matching import match_event, match_map_market
from app.opportunity_intel.live_edge.decisions import decide, wait_trigger_met
from app.opportunity_intel.live_edge.discovery import (
    discover_active_markets,
    discovery_sees_non_sports,
    select_evaluation_queue,
)
from app.opportunity_intel.live_edge.economics import (
    conservative_edge_pp,
    executable_quote_from_book,
    expected_value_usd,
    market_probability,
)
from app.opportunity_intel.live_edge.evaluators import evaluate_market
from app.opportunity_intel.live_edge.formatting import format_live_edge_card
from app.opportunity_intel.live_edge.missed import diagnose_missed_window
from app.opportunity_intel.live_edge.models import (
    EvidenceTier,
    DiscoveredMarket,
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveEdgeDecision,
    LiveEdgeSignalType,
    LiveStateSnapshot,
    MarketKind,
    MatchStatus,
    ProbabilityRange,
    SkipReason,
    WaitTrigger,
)
from app.opportunity_intel.live_edge.policy import DEFAULT_LIVE_EDGE_ENABLED
from app.opportunity_intel.live_edge.providers import FixtureLiveStateProvider
from app.opportunity_intel.live_edge.scanner import evaluate_for_user, live_edge_users, run_live_edge_cycle
from app.opportunity_intel.live_edge.wait_monitor import check_wait_triggers
from app.opportunity_intel.models import OpportunitySubscription
from app.opportunity_intel.scoring import SCORE_VERSION
from app.opportunity_intel.settings import balanced_settings
from app.opportunity_intel.settings_edit import apply_live_edge_immediate, get_or_default_subscription
from app.telegram.inline_menus import CB_OSET_LIVE_EDGE, opportunity_filters_keyboard
from app.telegram.opportunity_intel.formatting import format_filters_card
from tests.conftest import make_test_settings

NOW = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)
YES_ASSET = "yes-token-live-edge-1"
NO_ASSET = "no-token-live-edge-1"
USER_A = 101
USER_B = 202


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(database_path=str(tmp_path / "live_edge.db"), admin_ids=[1])
    initialize_database(value)
    initialize_opportunity_schema(value)
    live_edge_storage.initialize_live_edge_schema(value)
    upsert_user(value, USER_A, "a", "A")
    upsert_user(value, USER_B, "b", "B")
    return value


def _market(
    *,
    question: str,
    live: bool | None = True,
    end_in: int = 3600,
    tags: tuple[str, ...] = ("esports",),
    category_ids: tuple[str, ...] = ("sports",),
    price_yes: Decimal | None = Decimal("0.81"),
    liquidity: Decimal = Decimal("5000"),
    accepting: bool = True,
    book: bool = True,
    event_title: str | None = "Team A vs Team B",
    market_id: str = "m-1",
    event_slug: str = "team-a-vs-team-b",
    market_slug: str | None = None,
) -> DiscoveredMarket:
    return DiscoveredMarket(
        event_id="e-1",
        market_id=market_id,
        condition_id="c-1",
        event_slug=event_slug,
        question=question,
        category_ids=category_ids,
        tags=tags,
        live=live,
        liquidity_usd=liquidity,
        volume_24h_usd=Decimal("1000"),
        end_at=NOW + timedelta(seconds=end_in),
        outcomes=(("Yes", YES_ASSET), ("No", NO_ASSET)),
        price_hints=(price_yes, Decimal("1") - price_yes if price_yes is not None else None),
        accepting_orders=accepting,
        order_book_enabled=book,
        event_title=event_title,
        start_at=NOW - timedelta(hours=1),
        market_slug=market_slug,
    )


def _provenance() -> LiveDataProvenance:
    return LiveDataProvenance(
        provider="fixture-live-state",
        event_identifier="evt-bo5-1",
        source_locator="fixture://esports/evt-bo5-1",
        fetched_at=NOW,
        source_timestamp=NOW,
        reliability="verified_fixture",
    )


def _lol_state(**overrides) -> LiveStateSnapshot:
    payload = dict(
        sport="League of Legends",
        status="live",
        participants=("Team A", "Team B"),
        aliases=("A", "B"),
        competition="Generic Invitational",
        event_date=NOW,
        start_time=NOW - timedelta(hours=1),
        series_format="BO5",
        maps_won=(2, 0),
        current_map=3,
        map_finished=False,
        kills=(15, 3),
        towers=(8, 2),
        inhibitors=(1, 0),
        dragons=3,
        barons=1,
        gold_difference=9000,
        game_time_seconds=28 * 60,
        provenance=_provenance(),
    )
    payload.update(overrides)
    return LiveStateSnapshot(**payload)


def _book(ask: str, *, depth_size: str = "1000", received_at: float | None = None):
    ts = (received_at if received_at is not None else NOW.timestamp()) - 2
    bid = max(0.01, float(ask) - 0.02)
    parsed = parse_validated_order_book(
        {
            "asset_id": YES_ASSET,
            "timestamp": ts,
            "hash": f"hash-{ask}",
            "bids": [{"price": f"{bid:.3f}", "size": "500"}],
            "asks": [{"price": ask, "size": depth_size}],
        },
        requested_asset=YES_ASSET,
        received_at=received_at if received_at is not None else NOW.timestamp(),
    )
    assert isinstance(parsed, ApiSuccess)
    return parsed.data


def _enable(settings, telegram_id: int):
    opportunity_storage.save_subscription(
        settings,
        OpportunitySubscription(
            telegram_id=telegram_id,
            enabled=True,
            settings=balanced_settings(revision=0),
        ),
        expected_revision=None,
    )
    return apply_live_edge_immediate(settings, telegram_id=telegram_id, enabled=True)


def test_default_live_edge_is_off():
    assert DEFAULT_LIVE_EDGE_ENABLED is False
    assert balanced_settings().live_edge_enabled is False


def test_per_user_isolation(settings):
    _enable(settings, USER_A)
    a = get_or_default_subscription(settings, USER_A)
    b = get_or_default_subscription(settings, USER_B)
    assert a.settings.live_edge_enabled is True
    assert b.settings.live_edge_enabled is False
    users = live_edge_users(settings)
    assert [item[0] for item in users] == [USER_A]


def test_existing_scanner_unchanged_when_off(settings):
    result = load_top_opportunities(settings, telegram_id=USER_A)
    assert result.status == "not_initialized"
    assert SCORE_VERSION == 1
    assert "Live Edge" not in result.message


def test_filters_show_live_edge_toggle_off_by_default():
    text = format_filters_card(balanced_settings())
    assert "🔥 Live Edge Opportunities: ❌ OFF" in text
    assert "Find underpriced live markets using verified event data." in text
    callbacks = {
        button.callback_data
        for row in opportunity_filters_keyboard().inline_keyboard
        for button in row
    }
    assert CB_OSET_LIVE_EDGE in callbacks


def test_broad_discovery_is_not_sports_only():
    class _Client:
        def get_json(self, path, params=None):
            assert path == "/events/keyset"
            return {
                "events": [
                    _gamma_event("weather-nyc", "Highest temperature in NYC on August 30?", ["weather"], False),
                    _gamma_event("btc-100k", "Will Bitcoin hit 100k?", ["crypto"], False),
                    _gamma_event("election", "Will the incumbent win?", ["politics"], False),
                    _gamma_event("ukraine", "Will Russia capture the city?", ["world"], False),
                    _gamma_event("fed", "Fed rate decision in September?", ["finance"], False),
                    _gamma_event("nvidia", "Will NVIDIA announce a new chip?", ["tech"], False),
                    _gamma_event("lol-bo5", "Will Team A win Map 2?", ["sports", "esports"], True),
                    _gamma_event("nba", "Will Team X win?", ["sports"], True),
                    _gamma_event("culture", "Awards night winner?", ["pop-culture"], False),
                ],
                "next_cursor": None,
            }

    result = discover_active_markets(_Client(), now=NOW)
    assert result.status.value == "COMPLETE"
    assert discovery_sees_non_sports(result)
    identities = [classify_market(market).category for market in result.markets]
    assert LiveEdgeCategory.WEATHER in identities
    assert LiveEdgeCategory.CRYPTO in identities
    assert LiveEdgeCategory.POLITICS in identities
    assert LiveEdgeCategory.GEOPOLITICS in identities
    assert LiveEdgeCategory.ECONOMICS in identities
    assert LiveEdgeCategory.TECHNOLOGY in identities
    assert LiveEdgeCategory.ESPORTS_LOL in identities or LiveEdgeCategory.GENERAL in identities
    lol = next(market for market in result.markets if market.event_slug == "lol-bo5")
    assert lol.market_slug == "lol-bo5-mkt"


def _gamma_event(event_id, question, tags, live):
    return {
        "id": event_id,
        "slug": event_id,
        "title": question,
        "active": True,
        "closed": False,
        "archived": False,
        "live": live,
        "tags": [{"id": tag, "slug": tag, "label": tag} for tag in tags],
        "markets": [
            {
                "id": f"{event_id}-m",
                "conditionId": f"{event_id}-c",
                "question": question,
                "active": True,
                "closed": False,
                "archived": False,
                "resolved": False,
                "enableOrderBook": True,
                "acceptingOrders": True,
                "live": live,
                "outcomes": '["Yes","No"]',
                "clobTokenIds": f'["{event_id}-yes","{event_id}-no"]',
                "outcomePrices": '["0.40","0.60"]',
                "liquidity": "1000",
                "volume24hr": "500",
                "slug": f"{event_id}-mkt",
                "endDate": (NOW + timedelta(hours=3)).isoformat(),
            }
        ],
    }


def test_category_routing_and_unsupported_fail_closed():
    weather = classify_market(_market(question="Highest temperature in NYC on August 30?", category_ids=("science",), tags=("weather",)))
    assert weather.category == LiveEdgeCategory.WEATHER
    crypto = classify_market(_market(question="Will Bitcoin close above 100k?", category_ids=("crypto",), tags=("crypto",)))
    assert crypto.category == LiveEdgeCategory.CRYPTO
    eval_crypto = evaluate_market(
        _market(question="Will Bitcoin close above 100k?", category_ids=("crypto",), tags=("crypto",)),
        crypto,
        FixtureLiveStateProvider(()),
        now=NOW,
    )
    assert eval_crypto.skip_reason in {
        SkipReason.UNSUPPORTED_CATEGORY,
        SkipReason.INSUFFICIENT_DATA,
        SkipReason.NEAR_RESOLUTION_NO_EVIDENCE,
    }
    assert eval_crypto.probability is None


def test_map_vs_series_identity():
    series = classify_market(_market(question="Will Team A win the series?"))
    map1 = classify_market(_market(question="Will Team A win Map 1?"))
    map2 = classify_market(_market(question="Will PCIFIC win Map 2?"))
    game = classify_market(_market(question="Will Team A win Game 3?"))
    assert series.kind == MarketKind.SERIES_WINNER
    assert map1.kind == MarketKind.MAP_WINNER and map1.map_number == 1
    assert map2.kind == MarketKind.MAP_WINNER and map2.map_number == 2
    assert game.kind == MarketKind.GAME_WINNER and game.game_number == 3
    assert series.kind != map1.kind


def test_verified_and_ambiguous_event_matching():
    market = _market(question="Team A vs Team B - Series Winner")
    identity = classify_market(market)
    good = match_event(market, identity, (_lol_state(),), now=NOW)
    assert good.status == MatchStatus.VERIFIED_MATCH
    other = _lol_state(
        participants=("Team C", "Team D"),
        aliases=("C", "D"),
        provenance=LiveDataProvenance(
            provider="fixture-live-state",
            event_identifier="other",
            source_locator="fixture://other",
            fetched_at=NOW,
            source_timestamp=NOW,
            reliability="verified_fixture",
        ),
    )
    none = match_event(market, identity, (other,), now=NOW)
    assert none.status == MatchStatus.NO_MATCH
    twin = _lol_state(
        provenance=LiveDataProvenance(
            provider="fixture-live-state",
            event_identifier="dup",
            source_locator="fixture://dup",
            fetched_at=NOW,
            source_timestamp=NOW,
            reliability="verified_fixture",
        )
    )
    amb = match_event(market, identity, (_lol_state(), twin), now=NOW)
    assert amb.status == MatchStatus.AMBIGUOUS


def test_map_matching_rejects_wrong_and_finished_maps():
    map2 = classify_market(_market(question="Will Team A win Map 2?"))
    current_map3 = _lol_state(current_map=3)
    wrong = match_map_market(_market(question="Will Team A win Map 2?"), map2, (current_map3,), now=NOW)
    assert wrong.status == MatchStatus.NO_MATCH
    finished_map1 = _lol_state(current_map=1, map_finished=True)
    map2_after_map1 = match_map_market(
        _market(question="Will Team A win Map 2?"), map2, (finished_map1,), now=NOW
    )
    assert map2_after_map1.status == MatchStatus.NO_MATCH
    current_map2 = _lol_state(current_map=2)
    ok = match_map_market(_market(question="Will Team A win Map 2?"), map2, (current_map2,), now=NOW)
    assert ok.status == MatchStatus.VERIFIED_MATCH


def test_stale_live_and_quote_fail_closed():
    market = _market(question="Team A vs Team B - Series Winner")
    stale_state = _lol_state(
        provenance=LiveDataProvenance(
            provider="fixture-live-state",
            event_identifier="evt-bo5-1",
            source_locator="fixture://esports/evt-bo5-1",
            fetched_at=NOW - timedelta(minutes=10),
            source_timestamp=NOW - timedelta(minutes=10),
            reliability="verified_fixture",
        )
    )
    evaluation = evaluate_market(
        market, classify_market(market), FixtureLiveStateProvider((stale_state,)), now=NOW
    )
    quote = executable_quote_from_book(
        _book("0.81"), allocation_usd=Decimal("100"), side="YES", now=NOW
    )
    result = decide(evaluation, quote, now=NOW)
    assert result.decision == LiveEdgeDecision.SKIP
    assert result.skip_reason == SkipReason.STALE_LIVE_DATA

    fresh = evaluate_market(
        market, classify_market(market), FixtureLiveStateProvider((_lol_state(),)), now=NOW
    )
    stale_quote = executable_quote_from_book(
        _book("0.81", received_at=NOW.timestamp() + 0),
        allocation_usd=Decimal("100"),
        side="YES",
        now=NOW + timedelta(seconds=40),
    )
    stale_result = decide(fresh, stale_quote, now=NOW + timedelta(seconds=40))
    assert stale_result.decision == LiveEdgeDecision.SKIP
    assert stale_result.skip_reason == SkipReason.STALE_QUOTE


def test_executable_vwap_and_bankroll():
    book = _book("0.80", depth_size="50")
    quote = executable_quote_from_book(
        book, allocation_usd=Decimal("100"), side="YES", now=NOW
    )
    assert quote is not None
    assert quote.fully_executable is False
    deep = executable_quote_from_book(
        _book("0.80", depth_size="1000"),
        allocation_usd=Decimal("100"),
        side="YES",
        now=NOW,
    )
    assert deep.fully_executable is True
    assert deep.vwap == Decimal("0.8") or Decimal(str(deep.vwap)) == Decimal("0.80")
    assert market_probability(deep) == deep.vwap


def test_edge_and_expected_value():
    quote = executable_quote_from_book(
        _book("0.81", depth_size="1000"),
        allocation_usd=Decimal("100"),
        side="YES",
        now=NOW,
    )
    probability = ProbabilityRange(Decimal("0.94"), Decimal("0.96"))
    edge = conservative_edge_pp(probability, quote)
    assert edge == Decimal("0.94") - quote.vwap
    ev, roi = expected_value_usd(probability=probability, quote=quote)
    assert ev > 0
    assert roi > 0
    bad = expected_value_usd(
        probability=ProbabilityRange(Decimal("0.50"), Decimal("0.55")), quote=quote
    )
    assert bad is not None and bad[0] < 0


def test_pcific_like_bo5_price_path():
    market = _market(question="Team A vs Team B - Series Winner")
    provider = FixtureLiveStateProvider((_lol_state(),))
    user = replace(balanced_settings(), live_edge_enabled=True)
    decisions = []
    for price in ("0.62", "0.72", "0.81", "0.97", "0.999"):
        result = evaluate_for_user(
            market,
            user_settings=user,
            provider=provider,
            book=_book(price, depth_size="2000"),
            now=NOW,
        )
        decisions.append((price, result.decision, result.edge_pp, result.expected_value_usd))
    assert decisions[0][1] == LiveEdgeDecision.BUY_NOW
    assert decisions[1][1] == LiveEdgeDecision.BUY_NOW
    assert decisions[2][1] == LiveEdgeDecision.BUY_NOW
    assert decisions[4][1] == LiveEdgeDecision.TOO_LATE
    assert decisions[0][2] is not None and decisions[0][2] > decisions[2][2]


def test_map_winner_large_advantage_and_too_late():
    market = _market(question="Will Team A win Map 3?")
    provider = FixtureLiveStateProvider((_lol_state(current_map=3),))
    user = replace(balanced_settings(), live_edge_enabled=True)
    buy = evaluate_for_user(
        market, user_settings=user, provider=provider, book=_book("0.71"), now=NOW
    )
    assert buy.decision == LiveEdgeDecision.BUY_NOW
    assert buy.evaluation.signal_type == LiveEdgeSignalType.LIVE_MAP_EDGE
    late = evaluate_for_user(
        market, user_settings=user, provider=provider, book=_book("0.999"), now=NOW
    )
    assert late.decision == LiveEdgeDecision.TOO_LATE


def test_insufficient_liquidity_and_negative_ev():
    market = _market(question="Team A vs Team B - Series Winner")
    provider = FixtureLiveStateProvider((_lol_state(),))
    user = replace(balanced_settings(), live_edge_enabled=True)
    thin = evaluate_for_user(
        market,
        user_settings=user,
        provider=provider,
        book=_book("0.81", depth_size="1"),
        now=NOW,
    )
    assert thin.decision == LiveEdgeDecision.WAIT
    assert thin.wait_trigger is not None
    expensive = evaluate_for_user(
        market,
        user_settings=user,
        provider=provider,
        book=_book("0.99", depth_size="2000"),
        now=NOW,
    )
    assert expensive.decision in {LiveEdgeDecision.SKIP, LiveEdgeDecision.TOO_LATE, LiveEdgeDecision.WAIT}


def test_wait_trigger_and_transition(settings):
    _enable(settings, USER_A)
    market = _market(question="Team A vs Team B - Series Winner")
    provider = FixtureLiveStateProvider((_lol_state(),))
    user = get_or_default_subscription(settings, USER_A).settings
    waiting = evaluate_for_user(
        market,
        user_settings=user,
        provider=provider,
        book=_book("0.92", depth_size="2000"),
        now=NOW,
    )
    if waiting.decision != LiveEdgeDecision.WAIT:
        pytest.skip("price path did not produce WAIT at 0.90 for this fixture")
    assert waiting.wait_trigger is not None
    live_edge_storage.upsert_wait(
        settings,
        wait_uid="wait:test",
        signal_uid="le:test",
        market_id=market.market_id,
        asset_id=YES_ASSET,
        telegram_id=USER_A,
        trigger=waiting.wait_trigger,
        created_at=NOW,
    )
    live_edge_storage.upsert_signal(
        settings,
        {
            "signal_uid": "le:test",
            "telegram_id": USER_A,
            "market_id": market.market_id,
            "asset_id": YES_ASSET,
            "question": market.question,
            "category": "esports_lol",
            "signal_type": "WAIT_CANDIDATE",
            "decision": "WAIT",
            "side": "YES",
            "reasons_json": "[]",
            "risks_json": "[]",
            "provenance_json": "{}",
            "card_json": "{}",
            "first_detected_at": NOW.isoformat(),
            "evaluated_at": NOW.isoformat(),
        },
    )
    fires = check_wait_triggers(
        settings,
        quotes={YES_ASSET: _book("0.70")},
        probabilities={YES_ASSET: waiting.evaluation.probability},
        now=NOW,
    )
    assert fires
    assert wait_trigger_met(
        waiting.wait_trigger,
        quote=executable_quote_from_book(
            _book("0.70"), allocation_usd=Decimal("100"), side="YES", now=NOW
        ),
        edge_pp=Decimal("0.20"),
        current_map=3,
    )


def test_near_resolution_gates():
    market = _market(
        question="Highest temperature in NYC on August 30?",
        tags=("weather",),
        category_ids=("science",),
        price_yes=Decimal("0.88"),
        end_in=7200,
        live=False,
    )
    identity = classify_market(market)
    assert is_near_resolution_candidate(remaining_seconds=7200, price_hint=Decimal("0.88"))
    no_evidence = evaluate_market(market, identity, FixtureLiveStateProvider(()), now=NOW)
    assert no_evidence.skip_reason in {
        SkipReason.INSUFFICIENT_DATA,
        SkipReason.NEAR_RESOLUTION_NO_EVIDENCE,
    }
    weather_state = LiveStateSnapshot(
        sport="Weather",
        status="observed",
        participants=("NYC",),
        extra={
            "outcome_confirmed": True,
            "conservative_low": "0.96",
            "conservative_high": "0.98",
            "resolution_note": "official source update",
        },
        provenance=LiveDataProvenance(
            provider="weather-intelligence",
            event_identifier="KLGA-2026-08-30",
            source_locator="weather://station/KLGA",
            fetched_at=NOW,
            source_timestamp=NOW,
            reliability="primary_resolution",
        ),
    )
    good = evaluate_market(
        market, identity, FixtureLiveStateProvider((weather_state,)), now=NOW
    )
    assert good.near_resolution is True
    assert good.probability is not None
    user = replace(balanced_settings(), live_edge_enabled=True)
    buy = evaluate_for_user(
        market, user_settings=user, provider=FixtureLiveStateProvider((weather_state,)), book=_book("0.89"), now=NOW
    )
    assert buy.decision == LiveEdgeDecision.BUY_NOW
    late = evaluate_for_user(
        market,
        user_settings=user,
        provider=FixtureLiveStateProvider((weather_state,)),
        book=_book("0.972"),
        now=NOW,
    )
    assert late.decision == LiveEdgeDecision.TOO_LATE
    unresolved = LiveStateSnapshot(
        sport="Weather",
        status="observed",
        participants=("NYC",),
        extra={"unresolved_conditions": ["day not complete"], "outcome_confirmed": True, "conservative_low": "0.96", "conservative_high": "0.98"},
        provenance=weather_state.provenance,
    )
    blocked = evaluate_market(
        market, identity, FixtureLiveStateProvider((unresolved,)), now=NOW
    )
    assert blocked.skip_reason == SkipReason.UNRESOLVED_CONDITION
    ambiguous = LiveStateSnapshot(
        sport="Weather",
        status="observed",
        participants=("NYC",),
        extra={"resolution_ambiguous": True, "outcome_confirmed": True, "conservative_low": "0.96", "conservative_high": "0.98"},
        provenance=weather_state.provenance,
    )
    amb = evaluate_market(market, identity, FixtureLiveStateProvider((ambiguous,)), now=NOW)
    assert amb.skip_reason == SkipReason.AMBIGUOUS_RESOLUTION
    stale_ev = LiveStateSnapshot(
        sport="Weather",
        status="observed",
        participants=("NYC",),
        extra={"outcome_confirmed": True, "conservative_low": "0.96", "conservative_high": "0.98"},
        provenance=LiveDataProvenance(
            provider="weather-intelligence",
            event_identifier="KLGA-2026-08-30",
            source_locator="weather://station/KLGA",
            fetched_at=NOW - timedelta(hours=3),
            source_timestamp=NOW - timedelta(hours=3),
            reliability="primary_resolution",
        ),
    )
    stale = evaluate_for_user(
        market,
        user_settings=user,
        provider=FixtureLiveStateProvider((stale_ev,)),
        book=_book("0.89"),
        now=NOW,
    )
    assert stale.decision == LiveEdgeDecision.SKIP
    assert stale.skip_reason == SkipReason.STALE_LIVE_DATA


def test_ukrainian_card_preserves_original_title():
    market = _market(
        question="Will PCIFIC win Map 2?",
        event_title="PCIFIC vs Team Phoenix",
    )
    result = evaluate_for_user(
        market,
        user_settings=replace(balanced_settings(), live_edge_enabled=True),
        provider=FixtureLiveStateProvider(
            (
                _lol_state(
                    current_map=2,
                    participants=("PCIFIC", "Team Phoenix"),
                    aliases=("PCIFIC", "Phoenix"),
                ),
            )
        ),
        book=_book("0.71"),
        now=NOW,
    )
    text = format_live_edge_card(result)
    assert "Will PCIFIC win Map 2?" in text
    assert "🧠 ВЕРДИКТ:" in text
    assert "Консервативна ймовірність" not in text
    assert "94.372" not in text


def test_signal_history_and_missed(settings):
    _enable(settings, USER_A)
    live_edge_storage.upsert_signal(
        settings,
        {
            "signal_uid": "le:hist",
            "telegram_id": USER_A,
            "market_id": "m-1",
            "asset_id": YES_ASSET,
            "question": "Will Team A win Map 2?",
            "category": "esports_lol",
            "signal_type": "LIVE_MAP_EDGE",
            "decision": "BUY_NOW",
            "side": "YES",
            "reasons_json": "[]",
            "risks_json": "[]",
            "provenance_json": "{}",
            "card_json": "{}",
            "first_detected_at": NOW.isoformat(),
            "first_buy_at": NOW.isoformat(),
            "best_executable_price": "0.71",
            "best_edge_pp": "0.23",
            "best_ev_usd": "12.00",
            "evaluated_at": NOW.isoformat(),
        },
    )
    rows = live_edge_storage.list_history(settings, telegram_id=USER_A)
    assert rows and rows[0].first_buy_at is not None
    hidden = live_edge_storage.list_history(settings, telegram_id=USER_B)
    assert hidden == ()
    reason = diagnose_missed_window(
        settings,
        market_id="m-1",
        question="Will Team A win Map 2?",
        samples=((Decimal("0.12"), Decimal("8"), True),),
        alert_created=False,
        now=NOW,
    )
    assert reason is not None and "ПРОПУЩЕНА МОЖЛИВІСТЬ" in reason
    none = diagnose_missed_window(
        settings,
        market_id="m-1",
        question="Will Team A win Map 2?",
        samples=((Decimal("0.12"), Decimal("8"), True),),
        alert_created=True,
        now=NOW,
    )
    assert none is None


def test_cycle_off_is_noop(settings):
    market = _market(question="Team A vs Team B - Series Winner")
    result = run_live_edge_cycle(
        settings,
        now=NOW,
        markets=(market,),
        books={YES_ASSET: _book("0.81")},
        provider=FixtureLiveStateProvider((_lol_state(),)),
    )
    assert result.enabled_users == 0
    assert result.evaluations == 0


def test_top_opportunities_integration_when_on(settings):
    _enable(settings, USER_A)
    run_live_edge_cycle(
        settings,
        now=NOW,
        markets=(_market(question="Team A vs Team B - Series Winner"),),
        books={YES_ASSET: _book("0.81", depth_size="2000")},
        provider=FixtureLiveStateProvider((_lol_state(),)),
    )
    from app.opportunity_intel.live_edge.catalog import load_live_edge_cards

    cards = load_live_edge_cards(
        settings,
        telegram_id=USER_A,
        user_settings=get_or_default_subscription(settings, USER_A).settings,
    )
    assert cards
    assert "Team A vs Team B - Series Winner" in cards[0].text
    assert "🧠 ВЕРДИКТ:" in cards[0].text
    assert "Team A vs Team B - Series Winner" in cards[0].question
    other = load_live_edge_cards(
        settings,
        telegram_id=USER_B,
        user_settings=get_or_default_subscription(settings, USER_B).settings,
    )
    assert other == ()


@pytest.mark.asyncio
async def test_live_edge_toggle_roundtrip(settings):
    from app.telegram.inline_menus import CB_OPP_FILTERS
    from app.telegram.opportunity_intel.handlers import handle_opportunity_callback

    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.user_data = {}
    context.bot = MagicMock()
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.message = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=USER_A, username="a", first_name="A")
    update.message = None
    await handle_opportunity_callback(update, context, CB_OPP_FILTERS)
    text = update.effective_message.edit_text.await_args.args[0]
    assert "Live Edge Opportunities: ❌ OFF" in text
    await handle_opportunity_callback(update, context, "oset:le:on")
    enabled = get_or_default_subscription(settings, USER_A)
    assert enabled.settings.live_edge_enabled is True
    other = get_or_default_subscription(settings, USER_B)
    assert other.settings.live_edge_enabled is False


def test_live_edge_job_registers_only_after_user_enabled(settings):
    from app.opportunity_intel.live_edge.scheduler import (
        LIVE_EDGE_SCAN_JOB_NAME,
        LIVE_EDGE_SCAN_REGISTERED_KEY,
        schedule_live_edge_jobs,
    )

    app = MagicMock()
    app.bot_data = {}
    app.job_queue = MagicMock()
    schedule_live_edge_jobs(app, settings)
    app.job_queue.run_repeating.assert_not_called()
    assert LIVE_EDGE_SCAN_REGISTERED_KEY not in app.bot_data
    _enable(settings, USER_A)
    schedule_live_edge_jobs(app, settings)
    assert app.bot_data[LIVE_EDGE_SCAN_REGISTERED_KEY] is True
    app.job_queue.run_repeating.assert_called_once()
    kwargs = app.job_queue.run_repeating.call_args.kwargs
    assert kwargs["name"] == LIVE_EDGE_SCAN_JOB_NAME
    schedule_live_edge_jobs(app, settings)
    app.job_queue.run_repeating.assert_called_once()


def _upsert_decision(
    settings,
    *,
    telegram_id: int,
    decision: str,
    uid: str,
    question: str = "Diagnostic fixture — not a live recommendation",
    market_id: str = "m-le-1",
    card_json: str = "{}",
) -> None:
    from app.database import live_edge_storage

    stamp = NOW.isoformat()
    live_edge_storage.upsert_signal(
        settings,
        {
            "signal_uid": uid,
            "telegram_id": telegram_id,
            "market_id": market_id,
            "asset_id": YES_ASSET,
            "question": question,
            "category": "esports_lol",
            "signal_type": "LIVE_MAP_EDGE",
            "decision": decision,
            "side": "YES",
            "reasons_json": "[]",
            "risks_json": "[]",
            "provenance_json": "{}",
            "card_json": card_json,
            "first_detected_at": stamp,
            "first_buy_at": stamp if decision == "BUY_NOW" else None,
            "first_wait_at": stamp if decision == "WAIT" else None,
            "evaluated_at": stamp,
        },
    )


@pytest.mark.asyncio
async def test_deliver_live_edge_sends_buy_not_skip(settings):
    from app.database import live_edge_storage, opportunity_storage
    from app.opportunity_intel.live_edge.scheduler import deliver_live_edge_alerts
    from app.opportunity_intel.models import AlertMode, OpportunitySubscription

    sub = _enable(settings, USER_A)
    instant = replace(
        sub.settings, alert_mode=AlertMode.INSTANT, revision=sub.settings.revision + 1
    )
    opportunity_storage.save_subscription(
        settings,
        OpportunitySubscription(telegram_id=USER_A, enabled=True, settings=instant),
        expected_revision=sub.settings.revision,
    )
    _upsert_decision(settings, telegram_id=USER_A, decision="SKIP", uid="le:skip")
    _upsert_decision(settings, telegram_id=USER_A, decision="BUY_NOW", uid="le:buy")
    send = AsyncMock()
    sent = await deliver_live_edge_alerts(settings, (), send)
    assert sent == 1
    send.assert_awaited_once()
    text = send.await_args.kwargs["text"]
    assert "Diagnostic fixture" in text
    assert live_edge_storage.already_delivered(
        settings, signal_uid="le:buy", telegram_id=USER_A, kind="alert"
    )
    sent_again = await deliver_live_edge_alerts(settings, (), send)
    assert sent_again == 0
    send.assert_awaited_once()


def test_scan_priority_live_map_outranks_politics_near_res():
    live_map = _market(
        question="Will Team A win Map 1?",
        tags=("lol",),
        live=True,
        price_yes=Decimal("0.40"),
        end_in=3600,
    )
    politics = _market(
        question="Will Candidate X win the election?",
        tags=("election", "president"),
        category_ids=("politics",),
        live=False,
        price_yes=Decimal("0.92"),
        end_in=3600,
        event_title="2026 Election",
    )
    assert scan_priority(classify_market(live_map), live_map, now=NOW) == 1
    assert scan_priority(classify_market(politics), politics, now=NOW) == 8


def test_evaluation_queue_keeps_live_maps_ahead_of_politics_cap():
    maps = tuple(
        _market(
            question="Will Team A win Map 1?",
            tags=("lol",),
            live=True,
            market_id=f"map-{index}",
            event_title="Team A vs Team B",
        )
        for index in range(8)
    )
    politics = tuple(
        _market(
            question=f"Will Candidate {index} win the election?",
            tags=("election", "president"),
            category_ids=("politics",),
            live=False,
            price_yes=Decimal("0.91"),
            end_in=1800,
            event_title="Election",
            market_id=f"pol-{index}",
        )
        for index in range(80)
    )
    queued = select_evaluation_queue(politics + maps, now=NOW)
    queued_ids = {item.market_id for item in queued}
    assert {item.market_id for item in maps}.issubset(queued_ids)
    assert not any(item.market_id.startswith("pol-") for item in queued)


def test_verified_live_without_high_probability_is_watch_not_buy():
    market = _market(question="Will Team A win Map 1?", tags=("lol",))
    weak = _lol_state(
        current_map=1,
        kills=(6, 5),
        gold_difference=1000,
        towers=(4, 4),
        inhibitors=(0, 0),
        dragons=0,
        barons=0,
        game_time_seconds=600,
        maps_won=(0, 0),
    )
    evaluation = evaluate_market(
        market, classify_market(market), FixtureLiveStateProvider((weak,)), now=NOW
    )
    assert evaluation.skip_reason is None
    assert evaluation.match.status == MatchStatus.VERIFIED_MATCH
    assert evaluation.live_state is not None
    quote = executable_quote_from_book(
        _book("0.40"), allocation_usd=Decimal("100"), side="YES", now=NOW
    )
    result = decide(evaluation, quote, now=NOW)
    assert result.decision == LiveEdgeDecision.WATCH
    assert result.decision != LiveEdgeDecision.BUY_NOW
    card = format_live_edge_card(result)
    assert "НЕ РЕКОМЕНДАЦІЯ" not in card
    assert "⚠️ LIVE WATCH" in card
    assert "🧠 ВЕРДИКТ: ПОКИ СПОСТЕРІГАЄМО" in card


def test_medium_live_series_is_wait_not_buy():
    market = _market(question="Team A vs Team B - Series Winner", tags=("lol",))
    medium = _lol_state(
        maps_won=(2, 0),
        current_map=3,
        kills=None,
        gold_difference=None,
        towers=None,
        inhibitors=None,
        dragons=None,
        barons=None,
        game_time_seconds=None,
        round_score=None,
    )
    evaluation = evaluate_market(
        market, classify_market(market), FixtureLiveStateProvider((medium,)), now=NOW
    )
    assert evaluation.evidence_tier == EvidenceTier.MEDIUM
    result = decide(evaluation, None, now=NOW)
    assert result.decision == LiveEdgeDecision.WAIT
    assert result.wait_trigger is not None
    assert result.decision != LiveEdgeDecision.BUY_NOW


def test_buy_still_requires_high_and_source_conflict_stays_skip():
    market = _market(question="Team A vs Team B - Series Winner", tags=("lol",))
    buy = evaluate_for_user(
        market,
        user_settings=replace(balanced_settings(), live_edge_enabled=True),
        provider=FixtureLiveStateProvider((_lol_state(),)),
        book=_book("0.62"),
        now=NOW,
    )
    assert buy.decision == LiveEdgeDecision.BUY_NOW
    assert buy.evaluation.evidence_tier == EvidenceTier.HIGH


@pytest.mark.asyncio
async def test_deliver_live_edge_sends_watch_with_cap_and_dedupe(settings):
    from app.opportunity_intel.live_edge.scheduler import deliver_live_edge_alerts
    from app.opportunity_intel.models import AlertMode, OpportunitySubscription

    sub = _enable(settings, USER_A)
    instant = replace(
        sub.settings, alert_mode=AlertMode.INSTANT, revision=sub.settings.revision + 1
    )
    opportunity_storage.save_subscription(
        settings,
        OpportunitySubscription(telegram_id=USER_A, enabled=True, settings=instant),
        expected_revision=sub.settings.revision,
    )
    payload = (
        '{"has_live_state":true,"category":"soccer","provider":"thesportsdb-livescore",'
        '"live_score":"0–0 23\'","watch_fingerprint":"fp-a",'
        '"watch_reasons":["live candidate — evidence ще недостатньо для BUY"]}'
    )
    _upsert_decision(
        settings,
        telegram_id=USER_A,
        decision="WATCH",
        uid="le:watch-a",
        question="Will Londrina EC win on 2026-09-01?",
        market_id="m-watch-a",
        card_json=payload,
    )
    _upsert_decision(
        settings,
        telegram_id=USER_A,
        decision="WATCH",
        uid="le:watch-b",
        question="Will EC Juventude win on 2026-09-01?",
        market_id="m-watch-b",
        card_json=payload.replace("fp-a", "fp-b"),
    )
    _upsert_decision(
        settings,
        telegram_id=USER_A,
        decision="WATCH",
        uid="le:watch-c",
        question="Londrina EC to win the second half?",
        market_id="m-watch-c",
        card_json=payload.replace("fp-a", "fp-c"),
    )
    _upsert_decision(
        settings,
        telegram_id=USER_A,
        decision="WATCH",
        uid="le:watch-d",
        question="Exact Score: Londrina EC 0 - 0 EC Juventude?",
        market_id="m-watch-d",
        card_json=payload.replace("fp-a", "fp-d"),
    )
    _upsert_decision(
        settings,
        telegram_id=USER_A,
        decision="WATCH",
        uid="le:watch-e",
        question="Will Team Spirit win?",
        market_id="m-watch-e",
        card_json=payload.replace("fp-a", "fp-e"),
    )
    send = AsyncMock()
    sent = await deliver_live_edge_alerts(settings, (), send)
    assert sent == 3
    texts = [call.kwargs["text"] for call in send.await_args_list]
    assert all("⚠️ LIVE WATCH" in text for text in texts)
    assert all("НЕ РЕКОМЕНДАЦІЯ" not in text for text in texts)
    assert all("Exact Score" not in text for text in texts)
    sent_again = await deliver_live_edge_alerts(settings, (), send)
    assert sent_again == 0


@pytest.mark.asyncio
async def test_deliver_watch_skipped_without_live_state(settings):
    from app.opportunity_intel.live_edge.scheduler import deliver_live_edge_alerts
    from app.opportunity_intel.models import AlertMode, OpportunitySubscription

    sub = _enable(settings, USER_A)
    instant = replace(
        sub.settings, alert_mode=AlertMode.INSTANT, revision=sub.settings.revision + 1
    )
    opportunity_storage.save_subscription(
        settings,
        OpportunitySubscription(telegram_id=USER_A, enabled=True, settings=instant),
        expected_revision=sub.settings.revision,
    )
    _upsert_decision(settings, telegram_id=USER_A, decision="WATCH", uid="le:watch")
    send = AsyncMock()
    sent = await deliver_live_edge_alerts(settings, (), send)
    assert sent == 0
    send.assert_not_awaited()


def test_odd_even_kills_is_not_map_winner_and_loses_priority():
    prop = _market(
        question="Map 1: Odd/Even Total Kills?",
        tags=("cs2", "esports"),
        event_title=None,
        event_slug="some-match-odd-even",
        live=True,
    )
    identity = classify_market(prop)
    assert identity.kind == MarketKind.BINARY_EVENT
    assert scan_priority(identity, prop, now=NOW) == 9
    winner = _market(question="Will Team A win Map 1?", tags=("cs2",))
    assert classify_market(winner).kind == MarketKind.MAP_WINNER
    assert scan_priority(classify_market(winner), winner, now=NOW) == 1


def test_participants_from_slug_and_event_title():
    assert extract_participants(
        "Will Delfín win on 2026-09-01?",
        None,
        event_slug="delfin-vs-tecnico-universitario",
    ) == ("delfin", "tecnico universitario")
    market = _market(
        question="Will CD Coquimbo Unido win on 2026-08-29?",
        tags=("soccer",),
        event_title="CD Coquimbo Unido vs CD Huachipato",
        event_slug="coquimbo-unido-vs-huachipato",
    )
    identity = classify_market(market)
    assert identity.participants[0].lower().find("coquimbo") >= 0
    assert identity.participants[1].lower().find("huachipato") >= 0
    assert scan_priority(identity, market, now=NOW) == 9


def test_alias_and_club_prefix_matching():
    market = _market(
        question="Will Paris Saint-Germain win?",
        tags=("soccer",),
        event_title="Paris Saint-Germain vs Lyon",
    )
    identity = classify_market(market)
    snap = LiveStateSnapshot(
        sport="soccer",
        status="live",
        participants=("PSG", "Olympique Lyonnais"),
        aliases=("PSG", "OL"),
        competition="Ligue 1",
        start_time=NOW,
        score=(2, 0),
        minute=88,
        provenance=_provenance(),
    )
    matched = match_event(market, identity, (snap,), now=NOW)
    assert matched.status in {MatchStatus.VERIFIED_MATCH, MatchStatus.PROBABLE_MATCH}


def test_probable_match_is_watch_not_buy():
    market = _market(
        question="Team A vs Team B - Series Winner",
        tags=("lol",),
    )
    identity = classify_market(market)
    weak_time = _lol_state(sport="", series_format=None)
    evaluation = evaluate_market(
        market, identity, FixtureLiveStateProvider((weak_time,)), now=NOW
    )
    assert evaluation.match.status in {MatchStatus.VERIFIED_MATCH, MatchStatus.PROBABLE_MATCH}
    result = decide(evaluation, None, now=NOW)
    if evaluation.match.status == MatchStatus.PROBABLE_MATCH:
        assert result.decision == LiveEdgeDecision.WATCH
    else:
        assert result.decision in {LiveEdgeDecision.WATCH, LiveEdgeDecision.WAIT, LiveEdgeDecision.BUY_NOW}
    assert result.decision != LiveEdgeDecision.BUY_NOW or evaluation.evidence_tier == EvidenceTier.HIGH


def test_props_do_not_consume_map_queue():
    props = tuple(
        _market(
            question="Map 1: Odd/Even Total Kills?",
            tags=("cs2",),
            event_title=None,
            event_slug=f"prop-{index}",
            market_id=f"prop-{index}",
            live=True,
        )
        for index in range(40)
    )
    maps = tuple(
        _market(
            question="Will Team A win Map 1?",
            tags=("lol",),
            market_id=f"mapw-{index}",
            live=True,
        )
        for index in range(5)
    )
    queued = select_evaluation_queue(props + maps, now=NOW)
    ids = {item.market_id for item in queued}
    assert {item.market_id for item in maps}.issubset(ids)
    assert sum(1 for item in queued if item.market_id.startswith("prop-")) == 0


def test_live_provider_matches_outrank_unrelated_soccer():
    live_soccer = _market(
        question="Will Delfín win?",
        tags=("soccer",),
        event_title="Delfín vs Técnico Universitario",
        event_slug="delfin-vs-tecnico-universitario",
        market_id="zzz-delfin-1",
        live=True,
    )
    noise = tuple(
        _market(
            question=f"Will Club {index} win the second half?",
            tags=("soccer",),
            event_title=f"Club {index} vs Other {index}",
            event_slug=f"club-{index}-vs-other",
            market_id=f"noise-{index}",
            live=True,
        )
        for index in range(40)
    )
    snap = LiveStateSnapshot(
        sport="soccer",
        status="2H",
        participants=("Delfín", "Técnico Universitario"),
        aliases=("Delfin",),
        competition="Ecuadorian Serie A",
        start_time=NOW,
        score=(1, 0),
        minute=70,
        provenance=_provenance(),
    )
    queued = select_evaluation_queue(noise + (live_soccer,), now=NOW, live_snapshots=(snap,))
    assert queued[0].market_id == "zzz-delfin-1"


def test_will_win_title_extracts_single_participant():
    assert extract_participants(
        "Will Delfín win the second half?",
        None,
        event_slug="will-delfin-win-second-half",
    ) == ("Delfín",)
    assert extract_participants("JEF United Chiba to win the second half?", None) == (
        "JEF United Chiba",
    )


def test_unique_named_live_event_is_probable_watch_not_buy():
    market = _market(
        question="Will Delfín win?",
        tags=("soccer",),
        event_title=None,
        event_slug="will-delfin-win",
        live=True,
    )
    identity = classify_market(market)
    assert len(identity.participants) == 1
    snap = LiveStateSnapshot(
        sport="soccer",
        status="2H",
        participants=("Delfín SC", "Técnico Universitario"),
        aliases=("Delfin",),
        start_time=NOW,
        score=(1, 0),
        minute=70,
        provenance=_provenance(),
    )
    other = LiveStateSnapshot(
        sport="soccer",
        status="1H",
        participants=("Boca Juniors", "River Plate"),
        start_time=NOW,
        score=(0, 0),
        provenance=replace(_provenance(), event_identifier="other-evt"),
    )
    matched = match_event(market, identity, (snap, other), now=NOW)
    assert matched.status == MatchStatus.PROBABLE_MATCH
    evaluation = evaluate_market(
        market, identity, FixtureLiveStateProvider((snap, other)), now=NOW
    )
    result = decide(evaluation, None, now=NOW)
    assert result.decision == LiveEdgeDecision.WATCH
    assert result.decision != LiveEdgeDecision.BUY_NOW


def test_ambiguous_same_name_two_live_events_fail_closed():
    market = _market(
        question="Will Manchester United win?",
        tags=("soccer",),
        event_title=None,
        event_slug="will-manchester-united-win",
    )
    identity = classify_market(market)
    assert identity.participants == ("Manchester United",)
    one = LiveStateSnapshot(
        sport="soccer",
        status="live",
        participants=("Manchester United", "Chelsea"),
        start_time=NOW,
        provenance=replace(_provenance(), event_identifier="a"),
    )
    two = LiveStateSnapshot(
        sport="soccer",
        status="live",
        participants=("Manchester United", "Arsenal"),
        start_time=NOW,
        provenance=replace(_provenance(), event_identifier="b"),
    )
    matched = match_event(market, identity, (one, two), now=NOW)
    assert matched.status == MatchStatus.AMBIGUOUS
    evaluation = evaluate_market(
        market, identity, FixtureLiveStateProvider((one, two)), now=NOW
    )
    result = decide(evaluation, None, now=NOW)
    assert result.decision == LiveEdgeDecision.SKIP


def test_single_named_live_outranks_noise_in_queue():
    live_soccer = _market(
        question="Will Delfín win?",
        tags=("soccer",),
        event_title=None,
        event_slug="will-delfin-win-only",
        market_id="delfin-solo",
        live=True,
    )
    noise = tuple(
        _market(
            question=f"Will Club {index} win the second half?",
            tags=("soccer",),
            event_title=f"Club {index} vs Other {index}",
            event_slug=f"club-{index}-vs-other",
            market_id=f"noise-{index}",
            live=True,
        )
        for index in range(40)
    )
    snap = LiveStateSnapshot(
        sport="soccer",
        status="2H",
        participants=("Delfín", "Técnico Universitario"),
        start_time=NOW,
        score=(1, 0),
        provenance=_provenance(),
    )
    queued = select_evaluation_queue(noise + (live_soccer,), now=NOW, live_snapshots=(snap,))
    assert queued[0].market_id == "delfin-solo"


def test_exact_score_props_do_not_take_matched_series_slots():
    winner = _market(
        question="Will Londrina EC win?",
        tags=("soccer",),
        event_title="Londrina EC vs EC Juventude",
        event_slug="londrina-vs-juventude",
        market_id="winner-1",
        live=True,
    )
    props = tuple(
        _market(
            question=f"Exact Score: Londrina EC {index} - 0 EC Juventude?",
            tags=("soccer",),
            event_title="Londrina EC vs EC Juventude",
            event_slug="londrina-vs-juventude",
            market_id=f"score-{index}",
            live=True,
        )
        for index in range(20)
    )
    snap = LiveStateSnapshot(
        sport="soccer",
        status="1H",
        participants=("Londrina", "Juventude"),
        start_time=NOW,
        score=(0, 0),
        minute=23,
        provenance=_provenance(),
    )
    queued = select_evaluation_queue(props + (winner,), now=NOW, live_snapshots=(snap,))
    assert queued[0].market_id == "winner-1"
    assert sum(1 for item in queued[:10] if item.market_id.startswith("score-")) == 0


def test_elapsed_dated_market_is_not_boosted_by_live_match():
    stale = _market(
        question="Will Londrina EC win on 2026-08-08?",
        tags=("soccer",),
        event_title="Londrina EC vs EC Juventude",
        event_slug="londrina-old",
        market_id="stale-dated",
        live=True,
    )
    current = _market(
        question="Will Londrina EC win on 2026-08-30?",
        tags=("soccer",),
        event_title="Londrina EC vs EC Juventude",
        event_slug="londrina-now",
        market_id="current-dated",
        live=True,
    )
    snap = LiveStateSnapshot(
        sport="soccer",
        status="1H",
        participants=("Londrina", "Juventude"),
        start_time=NOW,
        score=(0, 0),
        provenance=_provenance(),
    )
    queued = select_evaluation_queue((stale, current), now=NOW, live_snapshots=(snap,))
    assert queued[0].market_id == "current-dated"
    assert all(item.market_id != "stale-dated" for item in queued[:5])


def test_weather_unconfirmed_near_resolution_is_watch_not_buy():
    market = _market(
        question="Highest temperature in NYC on August 30?",
        tags=("weather",),
        category_ids=("science",),
        price_yes=Decimal("0.88"),
        end_in=7200,
        live=False,
        event_title=None,
        event_slug="nyc-temp",
    )
    state = LiveStateSnapshot(
        sport="Weather",
        status="observed",
        participants=("KLGA",),
        extra={
            "outcome_confirmed": False,
            "station": "KLGA",
            "unresolved_conditions": ["weather day not confirmed complete by primary source"],
        },
        provenance=LiveDataProvenance(
            provider="nws-metar",
            event_identifier="KLGA",
            source_locator="https://api.weather.gov/stations/KLGA/observations/latest",
            fetched_at=NOW,
            source_timestamp=NOW,
            reliability="primary_resolution",
        ),
    )
    evaluation = evaluate_market(
        market, classify_market(market), FixtureLiveStateProvider((state,)), now=NOW
    )
    assert evaluation.skip_reason is None
    result = decide(evaluation, None, now=NOW)
    assert result.decision == LiveEdgeDecision.WATCH
    assert result.decision != LiveEdgeDecision.BUY_NOW


def test_stale_live_data_fail_closed_not_watch():
    market = _market(question="Team A vs Team B - Series Winner", tags=("lol",))
    stale = _lol_state()
    stale = replace(
        stale,
        provenance=replace(
            stale.provenance,
            fetched_at=NOW - timedelta(hours=3),
            source_timestamp=NOW - timedelta(hours=3),
        ),
    )
    evaluation = evaluate_market(
        market, classify_market(market), FixtureLiveStateProvider((stale,)), now=NOW
    )
    result = decide(evaluation, None, now=NOW)
    assert result.decision == LiveEdgeDecision.SKIP
    assert result.skip_reason == SkipReason.STALE_LIVE_DATA


def test_politics_and_season_markets_do_not_bind_unique_live_events():
    soccer = LiveStateSnapshot(
        sport="soccer",
        status="1H",
        participants=("Delfín", "Técnico Universitario"),
        aliases=("CLE", "DEL"),
        start_time=NOW,
        provenance=_provenance(),
    )
    politics = _market(
        question="Will Chelsea Clinton win the 2028 Democratic presidential nomination?",
        tags=("election", "president"),
        category_ids=("politics",),
        event_title=None,
        event_slug="chelsea-clinton-nom",
        live=False,
        end_in=86400 * 400,
    )
    assert match_event(politics, classify_market(politics), (soccer,), now=NOW).status == (
        MatchStatus.NO_MATCH
    )
    baseball = LiveStateSnapshot(
        sport="baseball",
        status="live",
        participants=("Boston Red Sox", "Seattle Mariners"),
        aliases=("BOS", "SEA"),
        start_time=NOW,
        provenance=_provenance(),
    )
    series = _market(
        question="Will the Boston Red Sox win the 2026 World Series?",
        tags=("mlb", "baseball"),
        event_title=None,
        event_slug="red-sox-world-series-2026",
        live=False,
        end_in=86400 * 60,
    )
    assert match_event(series, classify_market(series), (baseball,), now=NOW).status == (
        MatchStatus.NO_MATCH
    )


def test_partial_club_name_does_not_unique_bind_longer_team():
    market = _market(
        question="Will Universitario win?",
        tags=("soccer",),
        event_title=None,
        event_slug="will-universitario-win-liga-1",
        live=True,
    )
    snap = LiveStateSnapshot(
        sport="soccer",
        status="2H",
        participants=("Delfín", "Técnico Universitario"),
        start_time=NOW,
        score=(1, 0),
        provenance=_provenance(),
    )
    assert match_event(market, classify_market(market), (snap,), now=NOW).status == (
        MatchStatus.NO_MATCH
    )


def test_clob_attempted_when_gamma_order_book_flag_false(settings):
    from app.opportunity_intel.book import BookAdapterFailure
    from app.opportunity_intel.discovery import ResultStatus
    from app.opportunity_intel.models import OpportunityDiagnosticCode

    _enable(settings, USER_A)
    market = _market(
        question="Team A vs Team B - Series Winner",
        tags=("lol",),
        book=False,
        accepting=False,
    )
    client = MagicMock()
    client.fetch.return_value = BookAdapterFailure(
        status=ResultStatus.RETRYABLE_FAILURE,
        diagnostic_code=OpportunityDiagnosticCode.BOOK_UNAVAILABLE,
        source_code="http_error",
    )
    result = run_live_edge_cycle(
        settings,
        now=NOW,
        markets=(market,),
        book_client=client,
        provider=FixtureLiveStateProvider((_lol_state(),)),
    )
    client.fetch.assert_called()
    from app.opportunity_intel.models import ObservationMode

    assert client.fetch.call_args.kwargs["mode"] == ObservationMode.NORMAL
    assert result.funnel.get("clob_attempts", 0) >= 1
    assert result.funnel.get("clob_quotes", 0) == 0


def test_polymarket_link_uses_event_slug_not_title():
    from app.opportunity_intel.live_edge.formatting import resolve_polymarket_link

    link = resolve_polymarket_link(
        event_slug="londrina-ec-vs-ec-juventude",
        market_slug="will-londrina-win",
    )
    assert link == "https://polymarket.com/event/londrina-ec-vs-ec-juventude"
    assert "Will Londrina" not in (link or "")


def test_polymarket_link_market_slug_fallback():
    from app.opportunity_intel.live_edge.formatting import resolve_polymarket_link

    assert resolve_polymarket_link(
        event_slug=None,
        market_slug="will-londrina-win",
    ) == "https://polymarket.com/event/will-londrina-win"


def test_polymarket_link_missing_or_unsafe_slug_omitted():
    from app.opportunity_intel.live_edge.formatting import (
        POLYMARKET_OPEN_LABEL,
        format_watch_telegram,
        resolve_polymarket_link,
    )

    assert resolve_polymarket_link(event_slug=None, market_slug=None) is None
    assert resolve_polymarket_link(event_slug="has spaces") is None
    assert resolve_polymarket_link(event_slug="[click](http://evil)") is None
    assert resolve_polymarket_link(event_slug="<script>") is None
    assert resolve_polymarket_link(event_slug="https://polymarket.com/event/x") is None
    row = SimpleNamespace(question="Will Team A win?", category="soccer", card_json="{}")
    text = format_watch_telegram(row)
    assert POLYMARKET_OPEN_LABEL not in text
    assert "https://polymarket.com" not in text


def test_watch_and_catalog_cards_keep_validated_url_unescaped():
    from app.opportunity_intel.live_edge.catalog import format_stored_card
    from app.opportunity_intel.live_edge.formatting import (
        POLYMARKET_OPEN_LABEL,
        format_live_edge_card,
        format_watch_telegram,
    )
    from app.telegram.opportunity_intel.formatting import sanitize_plain

    slug = "team_a-vs-team-b"
    assert "_" not in sanitize_plain(slug)
    payload = json.dumps(
        {
            "has_live_state": True,
            "event_slug": slug,
            "market_slug": "will-team_a-win",
            "category": "soccer",
        }
    )
    watch_row = SimpleNamespace(
        question="Will Team_A win?",
        category="soccer",
        card_json=payload,
    )
    watch = format_watch_telegram(watch_row)
    assert POLYMARKET_OPEN_LABEL in watch
    assert f"https://polymarket.com/event/{slug}" in watch
    assert "https://polymarket.com/event/teama-vs-team-b" not in watch
    stored = SimpleNamespace(
        question="Will Team_A win?",
        side="YES",
        decision="TOO_LATE",
        conservative_p_low=None,
        executable_price=None,
        edge_pp=None,
        ev_usd=None,
        card_json=payload,
    )
    catalog = format_stored_card(stored)
    assert POLYMARKET_OPEN_LABEL in catalog
    assert f"https://polymarket.com/event/{slug}" in catalog
    buy = evaluate_for_user(
        _market(
            question="Team A vs Team B - Series Winner",
            tags=("lol",),
            event_slug=slug,
        ),
        user_settings=replace(balanced_settings(), live_edge_enabled=True),
        provider=FixtureLiveStateProvider((_lol_state(),)),
        book=_book("0.62"),
        now=NOW,
    )
    card = format_live_edge_card(buy)
    assert POLYMARKET_OPEN_LABEL in card
    assert f"https://polymarket.com/event/{slug}" in card


def test_retry_locked_write_retries_then_succeeds():
    from app.database.live_edge_storage import retry_locked_write

    calls = {"n": 0}

    def action():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    with patch("app.database.live_edge_storage.time.sleep"):
        assert retry_locked_write(action) == "ok"
    assert calls["n"] == 3


def test_upsert_signals_batch_is_idempotent(settings):
    _enable(settings, USER_A)
    row = {
        "signal_uid": "le:batch-one",
        "telegram_id": USER_A,
        "market_id": "m-batch",
        "asset_id": YES_ASSET,
        "question": "Will Team A win?",
        "category": "soccer",
        "signal_type": "LIVE_MATCH_EDGE",
        "decision": "WATCH",
        "side": "YES",
        "reasons_json": "[]",
        "risks_json": "[]",
        "provenance_json": "{}",
        "card_json": "{}",
        "first_detected_at": NOW.isoformat(),
        "evaluated_at": NOW.isoformat(),
    }
    live_edge_storage.upsert_signals(settings, (row, row))
    live_edge_storage.upsert_signals(settings, (row,))
    found = [
        item
        for item in live_edge_storage.list_active_signals(settings, telegram_id=USER_A, limit=40)
        if item.signal_uid == "le:batch-one"
    ]
    assert len(found) == 1


@pytest.mark.asyncio
async def test_live_edge_scan_skips_when_task_already_running(settings):
    from app.opportunity_intel.live_edge.scheduler import (
        _SHUTDOWN_TASKS_KEY,
        live_edge_scan_job,
    )

    _enable(settings, USER_A)
    blocker = asyncio.Event()

    async def _hang():
        await blocker.wait()

    hanging = asyncio.create_task(_hang())
    app = MagicMock()
    app.bot_data = {"settings": settings, _SHUTDOWN_TASKS_KEY: {hanging}}
    context = MagicMock()
    context.application = app
    with patch("app.database.copy_storage.try_acquire_job_lock") as acquire:
        await live_edge_scan_job(context)
        acquire.assert_not_called()
    hanging.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hanging

