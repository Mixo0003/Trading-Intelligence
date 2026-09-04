"""Short Ukrainian Live Edge / Momentum card formatting proofs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.opportunity_intel.live_edge.catalog import format_stored_card
from app.opportunity_intel.live_edge.formatting import (
    POLYMARKET_OPEN_LABEL,
    format_decision_telegram,
    format_watch_telegram,
)
from app.opportunity_intel.models import (
    MovementDirection,
    ObservationMode,
    OpportunityReasonCode,
    SignalFamily,
)
from app.opportunity_intel.payload import build_delivery_card_payload
from app.telegram.opportunity_intel.formatting import (
    format_opportunity_card,
    market_title_plain,
    sanitize_plain,
)

NOW = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)
TITLE = "Will Team_A win Map 2?"
SLUG = "team_a-vs-phoenix"


def _payload(**overrides):
    base = dict(
        opportunity_uid="uid-secret-should-hide",
        signal_at=NOW,
        wave_at=NOW + timedelta(seconds=3),
        signal_family=SignalFamily.EXECUTABLE_MOMENTUM,
        question=TITLE,
        outcome="Yes",
        event_slug=SLUG,
        mode=ObservationMode.NORMAL,
        canonical_score=Decimal("81.5"),
        reason_codes=(
            OpportunityReasonCode.FAST_5M_MOVE,
            OpportunityReasonCode.TIGHT_SPREAD,
            OpportunityReasonCode.LOW_SLIPPAGE,
            OpportunityReasonCode.HIGH_24H_VOLUME,
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


def _row(*, decision: str, card: dict, question: str = TITLE, **extra):
    payload = {
        "question": question,
        "decision": decision,
        "event_slug": SLUG,
        **card,
    }
    return SimpleNamespace(
        question=question,
        category=payload.get("category", "soccer"),
        decision=decision,
        side=payload.get("side"),
        executable_price=payload.get("best_ask"),
        card_json=json.dumps(payload),
        **extra,
    )


def test_market_title_keeps_underscores_and_strips_controls():
    assert market_title_plain("Will Team_A win?") == "Will Team_A win?"
    assert "\x00" not in market_title_plain("Will\x00 Team_A win?")
    assert "_" not in sanitize_plain("Will Team_A win?")


def test_watch_card_is_short_ukrainian_and_keeps_title_and_link():
    row = _row(
        decision="WATCH",
        card={
            "category": "soccer",
            "live_score": "0–0 23'",
            "best_ask": "0.42",
            "watch_reasons": [
                "live candidate — evidence ще недостатньо для BUY",
                "немає executable quote",
            ],
            "provider": "thesportsdb-livescore",
        },
    )
    text = format_watch_telegram(row)
    assert text.startswith("⚠️ LIVE WATCH")
    assert "НЕ РЕКОМЕНДАЦІЯ" not in text
    assert TITLE in text
    assert "Рахунок: 0–0 23'" in text
    assert "💵 Ціна: 0.42" in text
    assert "🧠 ВЕРДИКТ: ПОКИ СПОСТЕРІГАЄМО" in text
    assert "✅ live candidate — evidence ще недостатньо для BUY" in text
    assert "⚠️ немає executable quote" in text
    assert POLYMARKET_OPEN_LABEL in text
    assert f"https://polymarket.com/event/{SLUG}" in text
    assert "thesportsdb" not in text
    assert "Evidence" not in text
    assert "UTC" not in text


def test_wait_card_shows_trigger_and_omits_missing_price():
    row = _row(
        decision="WAIT",
        card={
            "live_score": "maps 2–0",
            "wait_trigger_uk": "ціна YES ≤ 0.55",
        },
    )
    text = format_decision_telegram(row)
    assert text.startswith("🟡 МОЖЛИВІСТЬ ПІД НАГЛЯДОМ")
    assert TITLE in text
    assert "Live: maps 2–0" in text
    assert "💵 Ціна:" not in text
    assert "🧠 ВЕРДИКТ: ЧЕКАТИ" in text
    assert "Зайти, якщо:" in text
    assert "🎯 ціна YES ≤ 0.55" in text
    assert POLYMARKET_OPEN_LABEL in text
    assert "None" not in text


def test_wait_card_uses_stored_trigger_json_when_payload_omits_it():
    row = SimpleNamespace(
        question=TITLE,
        category="soccer",
        decision="WAIT",
        side="YES",
        executable_price="0.63",
        wait_trigger_json=json.dumps(
            {
                "description_uk": "Зайти, якщо YES ask ≤ 0.70 та evidence стане HIGH",
                "max_yes_ask": "0.70",
            }
        ),
        card_json=json.dumps(
            {
                "question": TITLE,
                "decision": "WAIT",
                "event_slug": SLUG,
                "live_score": "2–0 88'",
                "best_ask": "0.63",
            }
        ),
    )
    text = format_decision_telegram(row)
    assert "Зайти, якщо:" in text
    assert "🎯 YES ask ≤ 0.70 та evidence стане HIGH" in text
    assert TITLE in text


def test_buy_card_caps_reasons_and_keeps_bankroll():
    row = _row(
        decision="BUY_NOW",
        card={
            "side": "YES",
            "vwap": "0.62",
            "allocation_usd": "100",
            "slippage": "0.012",
            "watch_reasons": ["reason one", "reason two", "reason three", "reason four"],
        },
    )
    text = format_stored_card(row)
    assert text.startswith("🚀 ГАРЯЧИЙ СИГНАЛ")
    assert TITLE in text
    assert "🎯 Ставка: YES" in text
    assert "💵 Вхід: 0.62" in text
    assert "💰 Бюджет: $100" in text
    assert "📉 Slippage: 1.20%" in text
    assert "🧠 ВЕРДИКТ: ЗАХОДИТИ" in text
    assert "✅ reason one" in text
    assert "✅ reason three" in text
    assert "reason four" not in text
    assert POLYMARKET_OPEN_LABEL in text


def test_too_late_card_uses_fixed_reason():
    row = _row(decision="TOO_LATE", card={"best_ask": "0.97"})
    text = format_stored_card(row)
    assert text.startswith("⚪ ВЖЕ ПІЗНО")
    assert TITLE in text
    assert "💵 Ціна: 0.97" in text
    assert "🧠 ВЕРДИКТ: НЕ ЗАХОДИТИ" in text
    assert "ринок уже врахував майже весь потенціал." in text
    assert POLYMARKET_OPEN_LABEL in text


def test_missing_optional_values_and_unsafe_slug_are_omitted():
    row = SimpleNamespace(
        question=TITLE,
        category="soccer",
        decision="WATCH",
        side=None,
        executable_price=None,
        card_json=json.dumps(
            {
                "question": TITLE,
                "decision": "WATCH",
                "event_slug": "bad slug with spaces",
                "market_slug": "<script>",
            }
        ),
    )
    text = format_watch_telegram(row)
    assert TITLE in text
    assert "💵 Ціна:" not in text
    assert "Рахунок:" not in text
    assert POLYMARKET_OPEN_LABEL not in text
    assert "https://polymarket.com" not in text
    assert "None" not in text
    assert "<script>" not in text


def test_telegram_escaping_sanitizes_reasons_not_title_or_url():
    row = _row(
        decision="WATCH",
        card={
            "watch_reasons": ["look *here*_and [click]"],
            "event_slug": SLUG,
        },
    )
    text = format_watch_telegram(row)
    assert TITLE in text
    assert "Team_A" in text
    assert "*" not in text.split(TITLE, 1)[1]
    assert "[" not in text.split(TITLE, 1)[1]
    assert f"https://polymarket.com/event/{SLUG}" in text


def test_momentum_card_matches_short_ukrainian_template():
    text = format_opportunity_card(_payload(), now=NOW)
    expected = "\n".join(
        [
            "📈 MOMENTUM СИГНАЛ",
            TITLE,
            "🎯 Сторона: Yes",
            "💵 Вхід: 0.5612",
            "📊 Score: 81.5",
            "💰 $250.00: VWAP 0.5612, slippage 1.23%",
            "🧠 ВЕРДИКТ:",
            "Ціна швидко рухається вгору.",
            "Чому:",
            "✅ швидкий рух за 5 хвилин",
            "✅ вузький спред",
            "✅ низький slippage",
            "🔗 Відкрити на Polymarket",
            f"https://polymarket.com/event/{SLUG}",
        ]
    )
    assert text == expected
    assert "FAST_5M_MOVE" not in text
    assert "uid-secret-should-hide" not in text
    assert "Disclaimer" not in text
    assert "UTC" not in text
    assert "HIGH_24H_VOLUME" not in text


def test_momentum_hides_invalid_link_and_keeps_title():
    text = format_opportunity_card(
        _payload(event_slug="bad slug with spaces"), now=NOW
    )
    assert TITLE in text
    assert POLYMARKET_OPEN_LABEL not in text
    assert "Open market:" not in text
    assert "None" not in text
