"""Human-readable Opportunity Telegram alert formatting proofs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.opportunity_intel.models import (
    MovementDirection,
    ObservationMode,
    OpportunityReasonCode,
    SignalFamily,
)
from app.opportunity_intel.payload import build_delivery_card_payload
from app.telegram.opportunity_intel.formatting import format_opportunity_card

NOW = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)


def _payload(**overrides):
    base = dict(
        opportunity_uid="uid-secret-should-hide",
        signal_at=NOW,
        wave_at=NOW + timedelta(seconds=3),
        signal_family=SignalFamily.EXECUTABLE_MOMENTUM,
        question="Will it rain in Paris?",
        outcome="Yes",
        event_slug="market-one",
        mode=ObservationMode.NORMAL,
        canonical_score=Decimal("81.5"),
        reason_codes=(
            OpportunityReasonCode.FAST_5M_MOVE,
            OpportunityReasonCode.TIGHT_SPREAD,
        ),
        direction=MovementDirection.UP,
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.05"),
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal("0.02"),
        relative_spread=Decimal("0.0363"),
        allocation_usd=Decimal("250"),
        execution_vwap=Decimal("0.5612"),
        slippage=Decimal("0.0123"),
        executable_ask_depth_usd=Decimal("1500"),
        volume_24h_usd=Decimal("250000"),
        liquidity_usd=Decimal("100000"),
        end_at=NOW + timedelta(hours=8),
        source_observed_at=NOW - timedelta(seconds=12),
    )
    base.update(overrides)
    return build_delivery_card_payload(**base)


def test_alert_text_is_human_readable_not_json():
    text = format_opportunity_card(_payload(), now=NOW)
    expected = "\n".join(
        [
            "📈 MOMENTUM СИГНАЛ",
            "Will it rain in Paris?",
            "🎯 Сторона: Yes",
            "💵 Вхід: 0.5612",
            "📊 Score: 81.5",
            "💰 $250.00: VWAP 0.5612, slippage 1.23%",
            "🧠 ВЕРДИКТ:",
            "Ціна швидко рухається вгору.",
            "Чому:",
            "✅ швидкий рух за 5 хвилин",
            "✅ вузький спред",
            "🔗 Відкрити на Polymarket",
            "https://polymarket.com/event/market-one",
        ]
    )
    assert text == expected
    assert "{" not in text
    assert "}" not in text
    assert "uid-secret-should-hide" not in text
    assert "wave_at" not in text
    assert "source_observed_at" not in text
    assert "event_slug" not in text
    assert "FAST_5M_MOVE" not in text
    assert "opportunity_uid" not in text


def test_alert_hides_link_when_slug_invalid_and_omits_none():
    text = format_opportunity_card(
        _payload(event_slug="bad slug with spaces"), now=NOW
    )
    assert "Open market:" not in text
    assert "🔗 Відкрити на Polymarket" not in text
    assert "None" not in text
    assert "Polymarket:" not in text
