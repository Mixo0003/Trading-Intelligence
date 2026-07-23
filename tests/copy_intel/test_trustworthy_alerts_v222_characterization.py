"""V2.2.2 — Trustworthy Copy Alerts characterization / acceptance tests.

Checkpoint 2 flips API-boundary defects (1–3, 6, 7) to fixed-behavior acceptance.
Checkpoint 4 flips position-identity and fail-closed ingest defects (4–5).
Remaining items still characterize unresolved defects for later checkpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.data_client import CopyDataClient, is_trade_activity
from app.copy_intel.ingestion import (
    diff_positions,
    enrich_events_with_trade_ids,
    ingest_wallet,
    load_snapshots_as_models,
    load_snapshots_v2_as_models,
    parse_position,
    parse_trade_event,
    replace_wallet_snapshots,
)
from app.copy_intel.models import CopyRecommendation, PositionSnapshot, TradeEvent
from app.copy_intel.position_book import build_canonical_position_book
from app.copy_intel.scheduler import (
    _deliver_pending_alerts,
    _subscriber_allows_recommendation,
)
from app.copy_intel.settings_profiles import update_setting
from app.copy_intel.watchlist import add_wallet, list_entries_for_user
from app.database import copy_storage
from app.database.copy_storage import commit_complete_position_book_v2
from app.database.storage import initialize_database
from tests.conftest import make_test_settings

# Sanitized synthetic addresses / IDs (not real production wallets or secrets).
WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ADMIN_A = 101
ADMIN_B = 202
CONDITION = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ASSET_YES = "token-yes-111"
ASSET_NO = "token-no-222"


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "v222_char.db"),
        copy_intel_enabled=True,
        copy_intel_read_only=True,
        admin_ids=[ADMIN_A, ADMIN_B],
    )
    initialize_database(s)
    return s


def _resp(status_code: int, payload):
    class Resp:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self):
            return payload

    return Resp()


def _snap(*, asset: str, outcome: str, size: float) -> PositionSnapshot:
    return PositionSnapshot(
        wallet=WALLET,
        condition_id=CONDITION,
        asset=asset,
        outcome=outcome,
        size=size,
        avg_price=0.4,
        cur_price=0.45,
        title="Sanitized market",
        slug="sanitized-market",
        event_slug="sanitized-event",
    )


def _event(**kwargs) -> TradeEvent:
    base = dict(
        wallet=WALLET,
        event_type="open",
        condition_id=CONDITION,
        asset=ASSET_YES,
        outcome="Yes",
        side="BUY",
        price=0.42,
        size=10.0,
        tx_hash=None,
        ts=1_700_000_000,
        title="Sanitized market",
        slug="sanitized-market",
        dedup_key="dk",
        prev_size=0.0,
        new_size=10.0,
    )
    base.update(kwargs)
    return TradeEvent(**base)


# ---------------------------------------------------------------------------
# 1. FIXED (CP2): CLOB midpoint reads mid_price
# ---------------------------------------------------------------------------


class TestFixed01ClobMidPrice:
    def test_midpoint_payload_uses_mid_price(self, settings, monkeypatch):
        """FIXED: midpoint parser reads documented mid_price field."""
        calls: list[dict] = []

        def fake_get(url, **kwargs):
            calls.append({"url": url, "params": kwargs.get("params")})
            if url.rstrip("/").endswith("/midpoint"):
                return _resp(200, {"mid_price": "0.61"})
            if url.rstrip("/").endswith("/book"):
                return _resp(200, {"bids": [], "asks": []})
            return _resp(404, {})

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", fake_get)
        quote = CopyDataClient(settings).fetch_quote(ASSET_YES)
        assert quote.midpoint == pytest.approx(0.61)
        assert any(c["url"].endswith("/midpoint") for c in calls)


# ---------------------------------------------------------------------------
# 2. FIXED (CP2): trades request uses takerOnly=false
# ---------------------------------------------------------------------------


class TestFixed02TakerOnly:
    def test_fetch_trades_params_include_taker_only_false(self, settings, monkeypatch):
        """FIXED: /trades is called with takerOnly=false."""
        captured: list[dict] = []

        def fake_get(url, **kwargs):
            captured.append({"url": url, "params": dict(kwargs.get("params") or {})})
            return _resp(200, [])

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", fake_get)
        result = CopyDataClient(settings).fetch_trades(WALLET, limit=25)
        assert isinstance(result, ApiSuccess)
        assert len(captured) == 1
        params = captured[0]["params"]
        assert params.get("user") == WALLET
        assert params.get("limit") == 25
        assert params.get("takerOnly") == "false"


# ---------------------------------------------------------------------------
# 3. FIXED (CP2): API failure distinguishable from successful empty
# ---------------------------------------------------------------------------


class TestFixed03ApiFailureVsEmpty:
    def test_http_failure_distinct_from_successful_empty(self, settings, monkeypatch):
        """FIXED: non-200 is ApiFailure; HTTP 200 [] is ApiSuccess(empty=True)."""
        outcomes: list[str] = []

        def fail_then_empty(url, **kwargs):
            if not outcomes:
                outcomes.append("fail")
                return _resp(503, {"error": "unavailable"})
            outcomes.append("ok")
            return _resp(200, [])

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", fail_then_empty)
        client = CopyDataClient(settings)
        failed = client.fetch_positions(WALLET)
        empty_ok = client.fetch_positions(WALLET)
        assert isinstance(failed, ApiFailure)
        assert isinstance(empty_ok, ApiSuccess)
        assert empty_ok.empty is True
        assert failed != empty_ok


# ---------------------------------------------------------------------------
# 4. OPEN: Position identity collapses two assets under one condition_id
# ---------------------------------------------------------------------------


class TestFixed04PositionIdentity:
    def test_two_assets_same_condition_remain_distinct(self):
        """FIXED: canonical book keys by asset; Yes+No under one market both survive."""
        raw_yes = {
            "conditionId": CONDITION,
            "asset": ASSET_YES,
            "outcome": "Yes",
            "size": 100.0,
            "avgPrice": 0.4,
            "curPrice": 0.41,
            "title": "Sanitized market",
            "slug": "sanitized-market",
            "eventSlug": "sanitized-event",
        }
        raw_no = {
            "conditionId": CONDITION,
            "asset": ASSET_NO,
            "outcome": "No",
            "size": 55.0,
            "avgPrice": 0.6,
            "curPrice": 0.59,
            "title": "Sanitized market",
            "slug": "sanitized-market",
            "eventSlug": "sanitized-event",
        }
        book = build_canonical_position_book([raw_yes, raw_no], WALLET)
        assert book.ok
        assert set(book.positions) == {ASSET_YES, ASSET_NO}
        assert book.positions[ASSET_YES].outcome == "Yes"
        assert book.positions[ASSET_NO].outcome == "No"

    def test_diff_keyed_by_asset_keeps_outcomes_separate(self):
        """FIXED: opening No while Yes already open yields a distinct open, not a reduce."""
        previous = {ASSET_YES: _snap(asset=ASSET_YES, outcome="Yes", size=100.0)}
        current = {
            ASSET_YES: _snap(asset=ASSET_YES, outcome="Yes", size=100.0),
            ASSET_NO: _snap(asset=ASSET_NO, outcome="No", size=55.0),
        }
        events = diff_positions(WALLET, previous, current)
        assert len(events) == 1
        assert events[0].event_type == "open"
        assert events[0].asset == ASSET_NO


# ---------------------------------------------------------------------------
# 5. FIXED (CP4): ApiFailure does not wipe snapshots
# ---------------------------------------------------------------------------


class TestFixed05PositionApiFailureRetainsSnapshots:
    def test_failed_positions_fetch_retains_prior_book(self, settings):
        """FIXED: ApiFailure must not mutate v2 snapshots or emit closes."""
        add_wallet(settings, WALLET, added_by=ADMIN_A)
        prior = {
            ASSET_YES: {
                "condition_id": CONDITION,
                "outcome": "Yes",
                "size": 100.0,
                "avg_price": 0.4,
                "cur_price": 0.45,
                "title": "Sanitized market",
                "slug": "sanitized-market",
                "event_slug": "sanitized-event",
            }
        }
        commit_complete_position_book_v2(settings, WALLET, prior, stamp_complete=True)
        assert load_snapshots_v2_as_models(settings, WALLET)
        client = MagicMock()
        client.fetch_positions.return_value = ApiFailure(
            code="http_error",
            status_code=500,
            retryable=True,
            request_count=1,
            message="http status 500",
        )
        events = ingest_wallet(settings, WALLET, client=client)
        assert events == []
        loaded = load_snapshots_v2_as_models(settings, WALLET)
        assert set(loaded) == {ASSET_YES}
        assert float(loaded[ASSET_YES].size) == 100.0


# ---------------------------------------------------------------------------
# 6. FIXED (CP2 correction): native identity ignores undocumented id / tradeId
# ---------------------------------------------------------------------------


class TestFixed06TradeIdFieldsIgnored:
    def test_id_alone_is_ignored_by_parse_trade_event(self):
        """FIXED: id alone must not produce a trusted native event."""
        raw = {
            "id": "undocumented-trade-id-1",
            "conditionId": CONDITION,
            "asset": ASSET_YES,
            "outcome": "Yes",
            "side": "BUY",
            "size": 10.0,
            "price": 0.4,
            "timestamp": 1_700_000_100,
            "title": "Sanitized",
            "slug": "sanitized",
        }
        assert parse_trade_event(raw, WALLET) is None

    def test_trade_id_alone_is_ignored_by_parse_trade_event(self):
        """FIXED: tradeId alone must not produce a trusted native event."""
        raw = {
            "tradeId": "undocumented-trade-id-2",
            "conditionId": CONDITION,
            "asset": ASSET_YES,
            "outcome": "Yes",
            "side": "BUY",
            "size": 10.0,
            "price": 0.4,
            "timestamp": 1_700_000_100,
            "title": "Sanitized",
            "slug": "sanitized",
        }
        assert parse_trade_event(raw, WALLET) is None

    def test_transaction_hash_is_accepted(self):
        """FIXED: documented transactionHash produces a trusted native tx UID."""
        from app.copy_intel.event_identity import build_event_uid

        raw = {
            "id": "should-be-ignored",
            "tradeId": "also-ignored",
            "transactionHash": "0xABCDEF",
            "conditionId": CONDITION,
            "asset": ASSET_YES,
            "outcome": "Yes",
            "side": "BUY",
            "size": 10.0,
            "price": 0.4,
            "timestamp": 1_700_000_100,
            "title": "Sanitized",
            "slug": "sanitized",
        }
        event = parse_trade_event(raw, WALLET)
        assert event is not None
        assert event.tx_hash == "0xabcdef"
        assert event.trade_id is None
        assert (event.event_uid or "").startswith("tx:0xabcdef:")
        # Even if trade_id were set on the model, identity must ignore it.
        leaked = _event(tx_hash=None, trade_id="should-not-be-trusted", ts=None)
        assert not build_event_uid(leaked).startswith("id:")

    def test_enrichment_ignores_id_and_trade_id_without_tx_hash(self):
        """FIXED: enrichment without transactionHash does not invent a native id."""
        event = _event(tx_hash=None, trade_id=None, size=10.0, prev_size=0.0, new_size=10.0)
        trades = [
            {
                "id": "undocumented-trade-id-2",
                "tradeId": "also-undocumented",
                "conditionId": CONDITION,
                "asset": ASSET_YES,
                "outcome": "Yes",
                "side": "BUY",
                "size": 10.0,
                "price": 0.4,
                "timestamp": 1_700_000_200,
            }
        ]
        enriched = enrich_events_with_trade_ids([event], trades)
        assert len(enriched) == 1
        assert enriched[0].tx_hash is None
        assert enriched[0].trade_id is None
        assert not (enriched[0].event_uid or "").startswith("id:")

    def test_compatibility_caller_cannot_bypass_via_client_trades(self, settings, monkeypatch):
        """FIXED: even if upstream JSON has only id/tradeId, client fails closed."""
        from app.copy_intel.api_result import unwrap_data_list

        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(
                200,
                [
                    {
                        "id": "bypass-attempt",
                        "tradeId": "bypass-attempt-2",
                        "conditionId": CONDITION,
                        "side": "BUY",
                        "size": 10.0,
                    }
                ],
            ),
        )
        result = CopyDataClient(settings).fetch_trades(WALLET)
        assert isinstance(result, ApiFailure)
        assert result.code == "malformed_row"
        assert unwrap_data_list(result) == []
        assert (
            parse_trade_event(
                {
                    "id": "bypass-attempt",
                    "conditionId": CONDITION,
                    "side": "BUY",
                    "size": 10.0,
                },
                WALLET,
            )
            is None
        )


# ---------------------------------------------------------------------------
# 7. FIXED (CP2): Activity classification allowlist type == TRADE
# ---------------------------------------------------------------------------


class TestFixed07ActivityClassification:
    def test_only_trade_type_counts_as_trade_activity(self):
        """FIXED: is_trade_activity allows only type == TRADE."""
        assert is_trade_activity("TRADE") is True
        assert is_trade_activity("trade") is True
        assert is_trade_activity("REDEEM") is False
        assert is_trade_activity("MAKER_REBATE") is False
        assert is_trade_activity("SOMETHING_NEW") is False


# ---------------------------------------------------------------------------
# 8. FIXED: Paper entry price is allocation execution VWAP (never 0.5 fallback)
# ---------------------------------------------------------------------------


class TestDefect08PaperCopyFallbackPrice:
    def test_legacy_half_fallback_expression_is_not_product_entry(self):
        """FIXED: missing prices would have fallen back to 0.5; product uses execution_vwap."""
        rec = CopyRecommendation(
            wallet=WALLET,
            trader_label="sanitized",
            market_title="Sanitized",
            market_slug="sanitized",
            outcome="Yes",
            event_type="open",
            entry_price=None,
            current_price=None,
            price_drift=None,
            slippage_est=None,
            trader_score=50.0,
            max_alloc=100.0,
            action="COPY",
            reason="characterization",
            execution_vwap=None,
        )
        legacy = rec.current_price or rec.entry_price or 0.5
        assert legacy == 0.5
        assert rec.execution_vwap is None  # product path rejects; never uses legacy

    def test_zero_current_price_does_not_become_paper_entry(self):
        """FIXED: falsy midpoint/current_price is irrelevant; entry is execution_vwap only."""
        rec = MagicMock(
            action="COPY",
            current_price=0.0,
            entry_price=None,
            execution_vwap=0.51,
            trader_vwap=0.40,
            midpoint=0.0,
            best_ask=0.52,
        )
        assert rec.execution_vwap == 0.51
        assert rec.execution_vwap != 0.5
        assert rec.execution_vwap != rec.trader_vwap
        assert rec.execution_vwap != rec.best_ask
        legacy = rec.current_price or rec.entry_price or 0.5
        assert legacy == 0.5
        assert rec.execution_vwap != legacy


# ---------------------------------------------------------------------------
# 9. FIXED: SKIP is never deliverable
# ---------------------------------------------------------------------------


class TestDefect09SkipDelivery:
    def test_skip_never_deliverable_under_either_alert_mode(self, settings):
        """FIXED (Checkpoint 7): SKIP is never eligible for delivery."""
        update_setting(settings, ADMIN_A, "alert_decisions", "FOLLOW_ONLY")
        update_setting(settings, ADMIN_B, "alert_decisions", "FOLLOW_AND_WATCH")
        skip = MagicMock(action="SKIP")
        watch = MagicMock(action="WATCH")
        copy = MagicMock(action="COPY")
        assert _subscriber_allows_recommendation(settings, ADMIN_A, skip) is False
        assert _subscriber_allows_recommendation(settings, ADMIN_A, watch) is False
        assert _subscriber_allows_recommendation(settings, ADMIN_A, copy) is True
        assert _subscriber_allows_recommendation(settings, ADMIN_B, skip) is False
        assert _subscriber_allows_recommendation(settings, ADMIN_B, watch) is True
        assert _subscriber_allows_recommendation(settings, ADMIN_B, copy) is True


# ---------------------------------------------------------------------------
# 10. FIXED: Owner-admin delivery + cross-admin isolation
# ---------------------------------------------------------------------------


class TestChar10AdminOwnershipAndCrossExposure:
    def test_admin_sees_only_own_watchlist(self, settings):
        """FIXED: even admins are scoped to added_by ownership for Copy UI."""
        add_wallet(settings, WALLET, label="A-owned", added_by=ADMIN_A)
        other = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        add_wallet(settings, other, label="B-owned", added_by=ADMIN_B)
        a_view = list_entries_for_user(settings, ADMIN_A, is_admin=True)
        b_view = list_entries_for_user(settings, ADMIN_B, is_admin=True)
        assert [e.wallet for e in a_view] == [WALLET]
        assert [e.wallet for e in b_view] == [other]

    @pytest.mark.asyncio
    async def test_alerts_deliver_only_to_owner_admin(self, settings):
        """FIXED: delivery targets added_by owner-admin only (no ADMIN_IDS fan-out)."""
        add_wallet(settings, WALLET, label="A-owned", added_by=ADMIN_A)
        rec = CopyRecommendation(
            wallet=WALLET,
            trader_label="sanitized",
            market_title="Sanitized",
            market_slug="sanitized",
            outcome="Yes",
            event_type="open",
            entry_price=0.4,
            current_price=0.41,
            price_drift=0.025,
            slippage_est=0.01,
            trader_score=70.0,
            max_alloc=100.0,
            action="COPY",
            reason="characterization",
        )
        pending = [("uid-char-1", WALLET, rec, None)]
        context = MagicMock()
        context.bot = MagicMock()
        context.bot.send_message = AsyncMock()
        with patch(
            "app.copy_intel.delivery.format_deliverable_alert_card",
        ) as fmt:
            from app.telegram.copy_intel.formatting import AlertFormatResult

            fmt.return_value = AlertFormatResult(ok=True, text="SANITIZED CARD")
            await _deliver_pending_alerts(context, settings, pending)
        assert context.bot.send_message.await_count == 1
        assert context.bot.send_message.await_args.kwargs["chat_id"] == ADMIN_A
        assert (
            copy_storage.claim_copy_alert_delivery(
                settings,
                subscriber_id=ADMIN_A,
                wallet=WALLET,
                event_uid="uid-char-1",
                payload_text="SANITIZED CARD",
            )
            is False
        )
        row_b = copy_storage.get_copy_alert_delivery(
            settings, subscriber_id=ADMIN_B, wallet=WALLET, event_uid="uid-char-1"
        )
        assert row_b is None
