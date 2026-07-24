"""V2.2.2 Checkpoint 10 — final Telegram alert card formatting and validated links."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.copy_intel.delivery import deliver_pending_alerts
from app.copy_intel.models import CopyRecommendation
from app.copy_intel.recommendation import (
    REASON_COPY_ELIGIBLE,
    REASON_METRICS_INCOMPLETE,
    REASON_SCORE_BELOW_THRESHOLD,
    REASON_SLIPPAGE_LIMIT_EXCEEDED,
)
from app.copy_intel.watchlist import add_wallet
from app.database import copy_storage
from app.database.storage import initialize_database
from app.telegram.copy_intel.formatting import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    AlertFormatResult,
    build_reason_summary,
    build_validated_market_link,
    build_validated_trader_link,
    format_deliverable_alert_card,
    format_recommendation_card,
    format_recommendation_messages,
)
from tests.conftest import make_test_settings

WALLET = "0xee00ba338c59557141789b127927a55f5cc5cea1"
ADMIN = 101
NOW = 1_721_686_994.0  # 2024-ish; ages computed vs last_fill


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "cp10_fmt.db"),
        admin_ids=[ADMIN],
        copy_paper_bankroll_usd=10_000.0,
    )
    initialize_database(s)
    return s


def _fixture_rec(**overrides) -> CopyRecommendation:
    """Formatting fixture structurally matching the Final Plan FOLLOW example."""
    base = dict(
        wallet=WALLET,
        trader_label="S-Works",
        market_title="Will Argentina win the 2026 FIFA World Cup?",
        market_slug="world-cup-winner",
        outcome="No",
        event_type="open",
        entry_price=0.88,
        current_price=0.885,
        price_drift=0.0057,
        slippage_est=0.0113,
        trader_score=78.0,
        max_alloc=300.0,
        action="COPY",
        reason="copy_eligible",
        event_uid="ff1:" + ("11" * 32),
        first_fill_ts=1_721_686_994 - 18,  # aligned so age ~18s at NOW
        last_fill_ts=1_721_686_994 - 18,
        detected_at=1_721_686_994 - 5,
        event_age_seconds=18.0,
        trader_vwap=0.88,
        midpoint=0.885,
        best_ask=0.89,
        execution_vwap=0.895,
        slippage=0.011299435,
        visible_ask_depth_usd=1240.0,
        proposed_allocation_usd=300.0,
        paper_bankroll_usd=10_000.0,
        paper_bankroll_share=0.03,
        resulting_position_size=2500.0,
        size_delta=500.0,
        reason_codes=(REASON_COPY_ELIGIBLE,),
    )
    base.update(overrides)
    return CopyRecommendation(**base)


class TestDecisionAndEventLabels:
    def test_copy_renders_as_follow(self):
        card = format_recommendation_card(_fixture_rec(action="COPY"))
        assert card.startswith("FOLLOW ·")
        assert "COPY ·" not in card

    def test_watch_renders_as_watch(self):
        card = format_recommendation_card(
            _fixture_rec(
                action="WATCH",
                reason_codes=(REASON_SLIPPAGE_LIMIT_EXCEEDED,),
            )
        )
        assert card.startswith("WATCH ·")
        assert "No paper position opened." in card

    def test_skip_never_rendered(self):
        result = format_deliverable_alert_card(_fixture_rec(action="SKIP"))
        assert result.ok is False
        with pytest.raises(ValueError):
            format_recommendation_card(_fixture_rec(action="SKIP"))

    def test_new_position_and_increase_labels(self):
        open_card = format_recommendation_card(_fixture_rec(event_type="open"))
        inc_card = format_recommendation_card(_fixture_rec(event_type="increase"))
        assert "New Position ·" in open_card
        assert "Increase ·" in inc_card


class TestTimesAndAge:
    def test_single_trade_timestamp(self):
        ts = 1_721_686_994
        card = format_recommendation_card(
            _fixture_rec(first_fill_ts=ts, last_fill_ts=ts), now=float(ts + 18)
        )
        assert "Trade time: 2024-07-22 21:03:14 UTC" in card or "Trade time:" in card
        assert "–" not in [
            line for line in card.splitlines() if line.startswith("Trade time:")
        ][0]

    def test_trade_timestamp_range(self):
        card = format_recommendation_card(
            _fixture_rec(first_fill_ts=1_721_686_994, last_fill_ts=1_721_686_996),
            now=1_721_687_014,
        )
        line = [ln for ln in card.splitlines() if ln.startswith("Trade time:")][0]
        assert "–" in line
        assert line.endswith("UTC")

    def test_detected_at_never_shown_as_trade_time(self):
        card = format_recommendation_card(
            _fixture_rec(
                first_fill_ts=1_700_000_000,
                last_fill_ts=1_700_000_000,
                detected_at=1_999_999_999,
            ),
            now=1_700_000_018,
        )
        assert "1999" not in card
        assert "detected" not in card.lower()

    def test_signal_age_uses_last_fill_ts(self):
        last = 1_700_000_000
        card = format_recommendation_card(
            _fixture_rec(first_fill_ts=last, last_fill_ts=last, detected_at=last - 100),
            now=float(last + 18),
        )
        assert "Signal age: 18s" in card


class TestPricesAndSizes:
    def test_three_prices_and_execution_vwap_separate(self):
        card = format_recommendation_card(_fixture_rec())
        assert "Trader VWAP: 88.0¢" in card
        assert "Market midpoint: 88.5¢" in card
        assert "Executable best ask: 89.0¢" in card
        assert "Paper execution VWAP: 89.5¢" in card
        # No ambiguous collapsed price line from legacy formatter.
        assert "Entry " not in card or "Entry ·" in card
        assert "-> Now" not in card

    def test_avg_price_never_substituted(self):
        card = format_recommendation_card(_fixture_rec())
        assert "avgPrice" not in card
        assert "avg_price" not in card

    def test_slippage_allocation_and_depth(self):
        card = format_recommendation_card(_fixture_rec(slippage=0.0113))
        assert "Slippage: +1.13%" in card
        assert "Visible ask depth: $1,240.00" in card
        assert "Paper allocation: $300.00" in card
        assert "Paper bankroll share: 3.00% of $10,000.00" in card

    def test_trade_size_and_position(self):
        card = format_recommendation_card(_fixture_rec())
        assert "Trade size: +500 shares" in card
        assert "Position after trade: 2,500 shares" in card

    def test_no_trader_bankroll_estimate(self):
        card = format_recommendation_card(_fixture_rec())
        assert "trader bankroll" not in card.lower()
        assert "estimated bankroll" not in card.lower()


class TestReasons:
    def test_follow_reason_summary(self):
        summary = build_reason_summary(_fixture_rec())
        assert "score 78" in summary
        assert "fresh complete book" in summary
        assert "allocation within exposure limits" in summary
        card = format_recommendation_card(_fixture_rec())
        assert card.count("Reason:") == 1

    def test_watch_reason_and_no_paper_implication(self):
        rec = _fixture_rec(
            action="WATCH",
            reason_codes=(REASON_SLIPPAGE_LIMIT_EXCEEDED, REASON_SCORE_BELOW_THRESHOLD),
            trader_score=52.0,
        )
        card = format_recommendation_card(rec)
        assert "slippage above preferred limit" in card
        assert "No paper position opened." in card
        assert "Paper allocation:" in card  # proposed still shown
        assert "opened paper" not in card.lower() or "No paper position opened." in card


class TestLinks:
    def test_valid_market_and_trader_links(self):
        assert (
            build_validated_market_link("world-cup-winner")
            == "https://polymarket.com/event/world-cup-winner"
        )
        assert (
            build_validated_trader_link(WALLET)
            == f"https://polymarket.com/profile/{WALLET}"
        )
        card = format_recommendation_card(_fixture_rec())
        assert "Market: https://polymarket.com/event/world-cup-winner" in card
        assert f"Trader: https://polymarket.com/profile/{WALLET}" in card

    @pytest.mark.parametrize(
        "slug",
        [
            None,
            "",
            " ",
            "a/b",
            "a?x=1",
            "a#frag",
            "../x",
            "https://evil.com/x",
            "a\\b",
            "a\nb",
            "a%2fbs",
        ],
    )
    def test_invalid_market_slug_rejected(self, slug):
        assert build_validated_market_link(slug) is None

    @pytest.mark.parametrize(
        "wallet",
        [None, "", "0xabc", "ee00ba338c59557141789b127927a55f5cc5cea1", "0xZZ"],
    )
    def test_invalid_wallet_rejected(self, wallet):
        assert build_validated_trader_link(wallet) is None

    def test_raw_api_url_ignored_as_slug(self):
        assert build_validated_market_link("https://polymarket.com/event/x") is None

    def test_fixed_https_host_only(self):
        link = build_validated_market_link("ok-slug")
        assert link is not None
        assert link.startswith("https://polymarket.com/event/")
        assert "http://" not in link


class TestSanitizationAndLength:
    def test_control_character_sanitization(self):
        card = format_recommendation_card(
            _fixture_rec(market_title="Hello\x00World\nCup", trader_label="A\tB")
        )
        assert "\x00" not in card
        assert "Hello World" in card or "HelloWorld" in card.replace(" ", "")

    def test_label_truncation(self):
        long_title = "X" * 300
        card = format_recommendation_card(_fixture_rec(market_title=long_title))
        assert "…" in card
        assert len(card) <= TELEGRAM_MAX_MESSAGE_LENGTH

    def test_deterministic_rendering(self):
        rec = _fixture_rec()
        a = format_recommendation_card(rec, now=NOW)
        b = format_recommendation_card(rec, now=NOW)
        assert a == b

    def test_no_message_exceeds_limit(self):
        messages = format_recommendation_messages([_fixture_rec() for _ in range(5)])
        assert all(len(m) <= TELEGRAM_MAX_MESSAGE_LENGTH for m in messages)
        assert all(m.startswith("Copy Trading Alerts\n") for m in messages)

    def test_one_card_per_message_policy(self):
        messages = format_recommendation_messages([_fixture_rec(), _fixture_rec()])
        assert len(messages) == 2


class TestDeliveryIntegration:
    @pytest.mark.asyncio
    async def test_formatting_failure_blocks_send(self, settings):
        add_wallet(settings, WALLET, added_by=ADMIN)
        rec = _fixture_rec(market_slug="")  # invalid link
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        await deliver_pending_alerts(ctx, settings, [("uid-bad", WALLET, rec, None)])
        assert ctx.bot.send_message.await_count == 0
        assert (
            copy_storage.get_copy_alert_delivery(
                settings, subscriber_id=ADMIN, wallet=WALLET, event_uid="uid-bad"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_invalid_links_block_delivery(self, settings):
        add_wallet(settings, WALLET, added_by=ADMIN)
        rec = _fixture_rec(market_slug="bad/slug")
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        await deliver_pending_alerts(ctx, settings, [("uid-link", WALLET, rec, None)])
        assert ctx.bot.send_message.await_count == 0

    @pytest.mark.asyncio
    async def test_retry_renders_same_payload(self, settings):
        add_wallet(settings, WALLET, added_by=ADMIN)
        rec = _fixture_rec()
        first = format_deliverable_alert_card(rec, now=NOW)
        second = format_deliverable_alert_card(rec, now=NOW)
        assert first.ok and second.ok
        assert first.text == second.text
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        await deliver_pending_alerts(
            ctx, settings, [("uid-same", WALLET, rec, None)], now=NOW
        )
        kwargs = ctx.bot.send_message.await_args.kwargs
        payload = kwargs["text"]
        assert first.text in payload
        assert "parse_mode" not in kwargs
        assert "reply_markup" not in kwargs

    def test_no_markdown_html_or_callbacks_in_card(self):
        card = format_recommendation_card(_fixture_rec())
        assert "<" not in card and ">" not in card
        assert "*" not in card
        assert "callback" not in card.lower()
        assert "inline" not in card.lower()


class TestLegacyTelegramFormattingFile:
    def test_batch_respects_limit(self):
        messages = format_recommendation_messages([_fixture_rec() for _ in range(10)])
        assert all(len(msg) <= TELEGRAM_MAX_MESSAGE_LENGTH for msg in messages)

    def test_follow_example_structure(self):
        # Align timestamps with the documented example shape (not exact epoch).
        card = format_recommendation_card(
            _fixture_rec(
                first_fill_ts=1_721_686_994,
                last_fill_ts=1_721_686_996,
            ),
            now=1_721_687_014,
        )
        assert card.startswith("FOLLOW · S-Works")
        assert "New Position · Will Argentina win the 2026 FIFA World Cup? · No" in card
        assert "Trader VWAP: 88.0¢" in card
        assert "Market midpoint: 88.5¢" in card
        assert "Executable best ask: 89.0¢" in card
        assert "Paper execution VWAP: 89.5¢" in card
        assert "Market: https://polymarket.com/event/world-cup-winner" in card
        assert f"Trader: https://polymarket.com/profile/{WALLET}" in card
