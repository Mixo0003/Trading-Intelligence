from app.copy_intel.models import CopyRecommendation
from app.telegram.copy_intel.formatting import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    format_recommendation_card,
    format_recommendation_messages,
)


def _sample_rec(action: str = "COPY") -> CopyRecommendation:
    return CopyRecommendation(
        wallet="0xee00ba338c59557141789b127927a55f5cc5cea1",
        trader_label="S-Works",
        market_title="Will Argentina win the 2026 FIFA World Cup?",
        market_slug="will-argentina-win",
        outcome="No",
        event_type="open",
        entry_price=0.88,
        current_price=0.58,
        price_drift=-0.34,
        slippage_est=0.01,
        trader_score=72.5,
        max_alloc=300.0,
        action=action,
        reason="score 72; drift -34.0%",
    )


class TestCopyTelegramFormatting:
    def test_card_is_concise(self):
        card = format_recommendation_card(_sample_rec())
        lines = [line for line in card.splitlines() if line.strip()]
        assert len(lines) <= 8

    def test_no_weather_terms(self):
        card = format_recommendation_card(_sample_rec())
        assert "WATCH" not in card or card.startswith("COPY") or card.startswith("WATCH")
        assert "forecast" not in card.lower()
        assert "temperature" not in card.lower()

    def test_batch_respects_limit(self):
        recs = [_sample_rec() for _ in range(30)]
        messages = format_recommendation_messages(recs)
        assert all(len(msg) <= TELEGRAM_MAX_MESSAGE_LENGTH for msg in messages)
        assert messages[0].startswith("Copy Trading Alerts")
