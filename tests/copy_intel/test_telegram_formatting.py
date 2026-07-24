"""Telegram copy alert formatting — V2.2.2 Checkpoint 10 final card shape."""

from app.copy_intel.models import CopyRecommendation
from app.copy_intel.recommendation import REASON_COPY_ELIGIBLE
from app.telegram.copy_intel.formatting import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    format_recommendation_card,
    format_recommendation_messages,
)

WALLET = "0xee00ba338c59557141789b127927a55f5cc5cea1"


def _sample_rec(action: str = "COPY") -> CopyRecommendation:
    return CopyRecommendation(
        wallet=WALLET,
        trader_label="S-Works",
        market_title="Will Argentina win the 2026 FIFA World Cup?",
        market_slug="will-argentina-win",
        outcome="No",
        event_type="open",
        entry_price=0.88,
        current_price=0.885,
        price_drift=0.0057,
        slippage_est=0.0113,
        trader_score=72.5,
        max_alloc=300.0,
        action=action,
        reason="copy_eligible",
        event_uid="ff1:" + ("22" * 32),
        first_fill_ts=1_721_686_994,
        last_fill_ts=1_721_686_994,
        trader_vwap=0.88,
        midpoint=0.885,
        best_ask=0.89,
        execution_vwap=0.895,
        slippage=0.0113,
        visible_ask_depth_usd=1240.0,
        proposed_allocation_usd=300.0,
        paper_bankroll_usd=10_000.0,
        paper_bankroll_share=0.03,
        resulting_position_size=2500.0,
        size_delta=500.0,
        reason_codes=(REASON_COPY_ELIGIBLE,),
    )


class TestCopyTelegramFormatting:
    def test_follow_card_shape(self):
        card = format_recommendation_card(_sample_rec(), now=1_721_687_012)
        assert card.startswith("FOLLOW · S-Works")
        assert "New Position ·" in card
        assert "Trader VWAP:" in card
        assert "Market midpoint:" in card
        assert "Executable best ask:" in card
        assert "Paper execution VWAP:" in card
        assert "Market: https://polymarket.com/event/will-argentina-win" in card
        assert f"Trader: https://polymarket.com/profile/{WALLET}" in card

    def test_copy_renders_as_follow_not_copy_label(self):
        card = format_recommendation_card(_sample_rec("COPY"))
        assert card.startswith("FOLLOW ·")
        assert not card.startswith("COPY ·")

    def test_no_weather_terms(self):
        card = format_recommendation_card(_sample_rec())
        assert "forecast" not in card.lower()
        assert "temperature" not in card.lower()

    def test_batch_respects_limit(self):
        recs = [_sample_rec() for _ in range(30)]
        messages = format_recommendation_messages(recs)
        assert all(len(msg) <= TELEGRAM_MAX_MESSAGE_LENGTH for msg in messages)
        assert messages[0].startswith("Copy Trading Alerts")
        assert len(messages) == 30  # one card per message
