import json
from pathlib import Path

import pytest

from app.copy_intel.data_client import (
    CopyDataClient,
    build_dedup_key,
    is_trade_activity,
    normalize_wallet,
)
from app.copy_intel.ingestion import diff_positions, parse_position, persist_trade_events
from app.copy_intel.models import PositionSnapshot, TradeEvent
from app.database.storage import initialize_database
from tests.conftest import make_test_settings

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "copy_intel_wallet_sworks.json"


@pytest.fixture
def settings(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "copy.db"))
    initialize_database(settings)
    return settings


@pytest.fixture
def fixture_data():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestWalletValidation:
    def test_normalize_wallet(self):
        wallet = normalize_wallet("0xEe00ba338c59557141789b127927a55f5cc5cea1")
        assert wallet == "0xee00ba338c59557141789b127927a55f5cc5cea1"

    def test_invalid_wallet_raises(self):
        with pytest.raises(ValueError):
            normalize_wallet("not-a-wallet")


class TestActivityFilter:
    def test_trade_activity_allowed(self):
        assert is_trade_activity("TRADE") is True

    def test_redeem_excluded(self):
        assert is_trade_activity("REDEEM") is False


class TestIngestionDiff:
    def test_open_position_detected(self, fixture_data):
        wallet = fixture_data["wallet"]
        raw = fixture_data["endpoints"]["positions"][0]
        current = {raw["conditionId"]: parse_position(raw, wallet)}
        events = diff_positions(wallet, {}, current)
        assert len(events) == 1
        assert events[0].event_type == "open"

    def test_increase_detected(self, fixture_data):
        wallet = fixture_data["wallet"]
        raw = fixture_data["endpoints"]["positions"][0]
        cid = raw["conditionId"]
        old = {
            cid: PositionSnapshot(
                wallet=wallet,
                condition_id=cid,
                asset=raw["asset"],
                outcome=raw["outcome"],
                size=100.0,
                avg_price=0.5,
                cur_price=0.5,
                title=raw["title"],
                slug=raw["slug"],
                event_slug=raw["eventSlug"],
            )
        }
        new_size = float(raw["size"])
        new = {
            cid: PositionSnapshot(
                wallet=wallet,
                condition_id=cid,
                asset=raw["asset"],
                outcome=raw["outcome"],
                size=new_size,
                avg_price=0.5,
                cur_price=0.5,
                title=raw["title"],
                slug=raw["slug"],
                event_slug=raw["eventSlug"],
            )
        }
        events = diff_positions(wallet, old, new)
        assert events[0].event_type == "increase"

    def test_close_detected(self, fixture_data):
        wallet = fixture_data["wallet"]
        raw = fixture_data["endpoints"]["positions"][0]
        cid = raw["conditionId"]
        old = {cid: parse_position(raw, wallet)}
        events = diff_positions(wallet, old, {})
        assert events[0].event_type == "close"


class TestDedup:
    def test_replay_same_event_not_inserted_twice(self, settings):
        event = TradeEvent(
            wallet="0xee00ba338c59557141789b127927a55f5cc5cea1",
            event_type="open",
            condition_id="0xabc",
            asset="1",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=10.0,
            tx_hash="0xtx",
            ts=123,
            title="Test",
            slug="test",
            dedup_key=build_dedup_key(
                "0xee00ba338c59557141789b127927a55f5cc5cea1",
                tx_hash="0xtx",
                asset="1",
                side="BUY",
                size=10.0,
                ts=123,
                event_type="open",
            ),
        )
        first = persist_trade_events(settings, [event])
        second = persist_trade_events(settings, [event])
        assert len(first) == 1
        assert len(second) == 0


class TestDataClientMocked:
    def test_fetch_positions_uses_data_api(self, settings, monkeypatch, fixture_data):
        wallet = fixture_data["wallet"]
        positions = fixture_data["endpoints"]["positions"]

        def fake_get(url, **kwargs):
            class Resp:
                status_code = 200

                def json(self):
                    return positions

            return Resp()

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", fake_get)
        client = CopyDataClient(settings)
        result = client.fetch_positions(wallet)
        assert len(result) == len(positions)
