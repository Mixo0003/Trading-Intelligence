"""V2.2.9 deterministic Polymarket trade-candidate policy tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.copy_intel.order_book import BookLevel, ValidatedOrderBook
from app.opportunity_intel.history import (
    FreshnessStatus,
    HistoryDirection,
    MovementResult,
)
from app.opportunity_intel.discovery import ResultStatus
from app.opportunity_intel.models import (
    ExecutionMetrics,
    MovementDirection,
    ObservationMode,
    OpportunityReasonCode,
    SignalFamily,
)
from app.opportunity_intel.live_trade_analysis import derive_outcome_leading
from app.opportunity_intel.payload import build_delivery_card_payload
from app.opportunity_intel.trade_candidate import (
    EvidenceDirection,
    FeeSchedule,
    LiveMatchState,
    MarketEvidence,
    ResolutionEvidence,
    SourceTier,
    TradeAction,
    TradeStage,
    build_trade_candidate,
    evaluate_market_evidence,
    sweep_sell_capacity,
    taker_fee_usd,
)
from app.telegram.opportunity_intel.formatting import format_trade_candidate_card

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _book(
    *, bid_size: float = 1000, ask_size: float = 1000, bid_price: float = 0.55
) -> ValidatedOrderBook:
    return ValidatedOrderBook(
        asset_id="asset-boomboys",
        book_hash="hash",
        quote_at=NOW.timestamp(),
        received_at=NOW.timestamp(),
        quote_age_seconds=0,
        bids=(BookLevel(price=bid_price, size=bid_size), BookLevel(price=bid_price - 0.01, size=bid_size)),
        asks=(BookLevel(price=max(0.56, bid_price + 0.01), size=ask_size),),
        best_bid=bid_price,
        best_ask=max(0.56, bid_price + 0.01),
        midpoint=(bid_price + max(0.56, bid_price + 0.01)) / 2,
        spread=max(0.56, bid_price + 0.01) - bid_price,
        visible_ask_depth_usd=1000,
        visible_bid_depth_usd=1000,
        min_order_size=5,
    )


def _movement(
    *,
    latest: str = "0.56",
    price_5m: str = "0.53",
    price_15m: str = "0.52",
    price_1h: str = "0.52",
    change_5m: str = "3",
    change_15m: str = "4",
    change_1h: str = "4",
    direction: HistoryDirection = HistoryDirection.UP,
) -> MovementResult:
    return MovementResult(
        asset_id="asset-boomboys",
        status=ResultStatus.COMPLETE,
        freshness=FreshnessStatus.FRESH,
        diagnostic_code=None,
        latest_price=Decimal(latest),
        latest_at=NOW,
        price_5m=Decimal(price_5m),
        price_15m=Decimal(price_15m),
        price_1h=Decimal(price_1h),
        change_5m_pp=Decimal(change_5m),
        change_15m_pp=Decimal(change_15m),
        change_1h_pp=Decimal(change_1h),
        absolute_change_5m_pp=abs(Decimal(change_5m)),
        absolute_change_15m_pp=abs(Decimal(change_15m)),
        absolute_change_1h_pp=abs(Decimal(change_1h)),
        direction=direction,
    )


def _execution(*, price: str = "0.56", slippage: str = "0.009") -> ExecutionMetrics:
    return ExecutionMetrics(
        asset_id="asset-boomboys",
        observed_at=NOW,
        allocation_usd=Decimal("100"),
        fully_executable=True,
        execution_vwap=Decimal(price),
        slippage=Decimal(slippage),
        quantity=Decimal("100") / Decimal(price),
        consumed_ask_usd=Decimal("100"),
    )


def _decision(**overrides):
    values = {
        "market_id": "market-boomboys",
        "event_slug": "dota-iron-wing-boomboys",
        "question": "Dota 2 — Iron Wing vs BoomBoys",
        "outcome": "BoomBoys",
        "category_ids": ("sports", "esports"),
        "mode": ObservationMode.LIVE_STRICT,
        "end_at": NOW + timedelta(hours=2),
        "now": NOW,
        "resolution": ResolutionEvidence(
            rules_text="This market resolves to the official match winner.",
            source_url="https://official.example/match",
        ),
        "live_state": LiveMatchState(
            event_slug="dota-iron-wing-boomboys",
            observed_at=NOW,
            live=True,
            ended=False,
            score="0-1",
            period="Map 2",
            elapsed="18:30",
            outcome_leading=True,
        ),
        "evidence": (),
        "movement": _movement(),
        "book": _book(),
        "entry_execution": _execution(),
        "fee_schedule": FeeSchedule(enabled=True, rate=Decimal("0.03")),
        "maximum_slippage": Decimal("0.02"),
    }
    values.update(overrides)
    return build_trade_candidate(**values)


def test_executed_impulse_matches_requested_wait_contract():
    decision = _decision()
    assert decision.action == TradeAction.WAIT
    assert decision.stage == TradeStage.EXECUTED_IMPULSE
    assert decision.maximum_entry_price == Decimal("0.5300")
    assert decision.target_price == Decimal("0.6000")
    assert "Не входити зараз" in decision.conclusion
    assert decision.projection is not None


def test_small_confirmed_live_move_can_be_entry_trigger():
    movement = _movement(
        latest="0.56",
        price_5m="0.55",
        price_15m="0.55",
        price_1h="0.55",
        change_5m="1",
        change_15m="1",
        change_1h="1",
    )
    decision = _decision(movement=movement)
    assert decision.action == TradeAction.ENTER
    assert decision.stage == TradeStage.ENTRY_TRIGGER
    assert decision.projection is not None
    assert decision.projection.net_return >= Decimal("0.02")


def test_prior_signal_target_can_produce_exit_take_profit():
    book = _book(bid_price=0.60)
    execution = _execution(price="0.61")
    decision = _decision(
        book=book,
        entry_execution=execution,
        target_price_hint=Decimal("0.60"),
    )
    assert decision.action == TradeAction.EXIT
    assert decision.stage == TradeStage.EXIT_TAKE_PROFIT


def test_exit_is_never_emitted_for_negative_target_projection():
    decision = _decision(
        book=_book(bid_price=0.60),
        entry_execution=_execution(price="0.61"),
        target_price_hint=Decimal("0.50"),
    )
    assert decision.action == TradeAction.SKIP
    assert decision.stage == TradeStage.SKIP
    assert decision.projection is not None
    assert decision.projection.net_profit_usd < 0


def test_missing_live_state_for_sports_forces_wait():
    decision = _decision(live_state=None)
    assert decision.action == TradeAction.WAIT
    assert decision.stage == TradeStage.WAIT
    assert "live-стан" in decision.conclusion


def test_geopolitical_no_deadline_can_supply_non_news_forward_catalyst():
    movement = _movement(
        latest="0.56",
        price_5m="0.55",
        price_15m="0.55",
        price_1h="0.55",
        change_5m="1",
        change_15m="1",
        change_1h="1",
    )
    decision = _decision(
        event_slug="nato-russia-clash-august-2026",
        question="NATO x Russia military clash by August 31, 2026?",
        outcome="No",
        category_ids=("geopolitics", "nato", "russia"),
        mode=ObservationMode.NORMAL,
        end_at=NOW + timedelta(days=10),
        resolution=ResolutionEvidence(
            rules_text=(
                "This market resolves No otherwise. The resolution source for "
                "this market will be a consensus of credible reporting."
            ),
            source_url="",
        ),
        live_state=None,
        movement=movement,
        fee_schedule=FeeSchedule(enabled=False, rate=Decimal("0")),
    )
    assert decision.action == TradeAction.ENTER
    assert decision.stage == TradeStage.ENTRY_TRIGGER
    assert any(item.value == "DEADLINE_DECAY" for item in decision.catalysts)


def test_missing_rules_or_exit_depth_forces_skip():
    missing_rules = _decision(
        resolution=ResolutionEvidence(rules_text="", source_url="")
    )
    assert missing_rules.action == TradeAction.SKIP
    thin = _decision(book=_book(bid_size=1))
    assert thin.action == TradeAction.SKIP
    assert "вийти" in thin.conclusion


def test_fee_curve_and_sell_sweep_are_decimal_and_capacity_aware():
    fee = taker_fee_usd(
        shares=Decimal("100"),
        price=Decimal("0.5"),
        schedule=FeeSchedule(enabled=True, rate=Decimal("0.03")),
    )
    assert fee == Decimal("0.75000")
    enough = sweep_sell_capacity(_book(), Decimal("100"))
    assert enough.fully_executable is True
    assert enough.vwap == Decimal("0.55")
    not_enough = sweep_sell_capacity(_book(bid_size=1), Decimal("3000"))
    assert not_enough.fully_executable is False


def test_official_sports_score_maps_exact_team_outcome_conservatively():
    assert (
        derive_outcome_leading(
            "Dota 2: Iron Wing vs BoomBoys (BO3)", "BoomBoys", "0-1"
        )
        is True
    )
    assert (
        derive_outcome_leading(
            "Dota 2: Iron Wing vs BoomBoys (BO3)", "Iron Wing", "0-1"
        )
        is False
    )
    assert (
        derive_outcome_leading(
            "Dota 2: Iron Wing vs BoomBoys (BO3)", "Yes", "0-1"
        )
        is None
    )


def test_evidence_requires_exact_official_or_two_trusted_and_conflicts_wait():
    def item(source_id: str, tier: SourceTier, direction=EvidenceDirection.SUPPORTS):
        return MarketEvidence(
            source_id=source_id,
            source_name=source_id,
            source_url=f"https://{source_id}.example/item",
            tier=tier,
            direction=direction,
            published_at=NOW - timedelta(minutes=5),
            market_id="market-boomboys",
            event_slug="dota-iron-wing-boomboys",
            outcome="BoomBoys",
            entity_match=True,
            date_match=True,
            outcome_match=True,
        )

    official = evaluate_market_evidence(
        [item("league", SourceTier.OFFICIAL)],
        now=NOW,
        market_id="market-boomboys",
        event_slug="dota-iron-wing-boomboys",
        outcome="BoomBoys",
    )
    assert official.confirmed is True
    one_trusted = evaluate_market_evidence(
        [item("reuters", SourceTier.TRUSTED)],
        now=NOW,
        market_id="market-boomboys",
        event_slug="dota-iron-wing-boomboys",
        outcome="BoomBoys",
    )
    assert one_trusted.confirmed is False
    two_trusted = evaluate_market_evidence(
        [item("reuters", SourceTier.TRUSTED), item("ap", SourceTier.TRUSTED)],
        now=NOW,
        market_id="market-boomboys",
        event_slug="dota-iron-wing-boomboys",
        outcome="BoomBoys",
    )
    assert two_trusted.confirmed is True
    conflict = evaluate_market_evidence(
        [
            item("league", SourceTier.OFFICIAL),
            item("reuters", SourceTier.TRUSTED, EvidenceDirection.CONFLICTS),
        ],
        now=NOW,
        market_id="market-boomboys",
        event_slug="dota-iron-wing-boomboys",
        outcome="BoomBoys",
    )
    assert conflict.conflict is True
    assert conflict.confirmed is False


def test_telegram_trade_candidate_card_uses_requested_ukrainian_structure():
    decision = _decision()
    payload = build_delivery_card_payload(
        opportunity_uid="opp-v229",
        signal_at=NOW,
        wave_at=NOW,
        signal_family=SignalFamily.EXECUTABLE_MOMENTUM,
        question="Dota 2 — Iron Wing vs BoomBoys",
        outcome="BoomBoys",
        event_slug="dota-iron-wing-boomboys",
        mode=ObservationMode.LIVE_STRICT,
        canonical_score=Decimal("85"),
        reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
        direction=MovementDirection.UP,
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.04"),
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.555"),
        absolute_spread=Decimal("0.01"),
        relative_spread=Decimal("0.018"),
        allocation_usd=Decimal("100"),
        execution_vwap=Decimal("0.56"),
        slippage=Decimal("0.009"),
        executable_ask_depth_usd=Decimal("1000"),
        volume_24h_usd=Decimal("100000"),
        liquidity_usd=Decimal("50000"),
        end_at=NOW + timedelta(hours=2),
        source_observed_at=NOW,
    )
    text = format_trade_candidate_card(payload, decision)
    assert text.startswith("POLYMARKET TRADE CANDIDATE")
    assert "Рішення: WAIT" in text
    assert "Стан: EXECUTED_IMPULSE" in text
    assert "Допустимий вхід: ≤ 0.5300" in text
    assert "Ціль продажу: 0.6000" in text
    assert "Після комісій і прослизання" in text
    assert "бот не створює реальних ордерів" in text
