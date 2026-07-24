import pytest

from app.copy_intel.models import TradeEvent
from app.copy_intel.recommendation import (
    REASON_AUTHORITATIVE_EVENT_REQUIRED,
    REASON_EVENT_TOO_OLD,
    build_recommendation,
)
from app.copy_intel.risk import assert_read_only, check_exposure_limits, record_paper_copy
from app.database.storage import initialize_database
from tests.conftest import make_test_settings


class TestReadOnlyGuard:
    def test_read_only_required(self):
        settings = make_test_settings(copy_intel_read_only=True)
        assert_read_only(settings)

    def test_non_read_only_raises(self):
        settings = make_test_settings(copy_intel_read_only=False)
        with pytest.raises(RuntimeError):
            assert_read_only(settings)


class TestExposureLimits:
    def test_per_market_limit(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "risk.db"),
            copy_paper_bankroll_usd=10000,
        )
        initialize_database(settings)
        wallet = "0xee00ba338c59557141789b127927a55f5cc5cea1"
        condition = "0xmarket1"
        ok, _, cap = check_exposure_limits(
            settings, wallet=wallet, condition_id=condition, proposed_notional=600
        )
        assert ok is True
        assert cap == 300.0

    def test_total_sleeve_limit(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "risk2.db"),
            copy_paper_bankroll_usd=10000,
        )
        initialize_database(settings)
        wallet = "0xee00ba338c59557141789b127927a55f5cc5cea1"
        for i in range(4):
            record_paper_copy(
                settings,
                wallet=wallet,
                condition_id=f"0xcond{i}",
                market_slug=f"m{i}",
                outcome="Yes",
                notional=400,
                entry_price=0.5,
            )
        ok, reason, cap = check_exposure_limits(
            settings, wallet=wallet, condition_id="0xcond9", proposed_notional=300
        )
        assert ok is False or cap < 300


class TestRecommendationDowngrades:
    def test_non_authoritative_event_skips(self, tmp_path, monkeypatch):
        settings = make_test_settings(
            database_path=str(tmp_path / "rec.db"),
            copy_max_trade_age_min=30,
            copy_min_resolved_trades=5,
        )
        initialize_database(settings)

        class FakeMetrics:
            eligible = True
            ineligible_reason = None

        monkeypatch.setattr(
            "app.copy_intel.recommendation.compute_trader_metrics",
            lambda *a, **k: FakeMetrics(),
        )
        monkeypatch.setattr("app.copy_intel.recommendation.score_trader", lambda m: 80.0)

        event = TradeEvent(
            wallet="0xee00ba338c59557141789b127927a55f5cc5cea1",
            event_type="open",
            condition_id="0xabc",
            asset="token",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=100.0,
            tx_hash=None,
            ts=1,
            title="Test market",
            slug="test-market",
            dedup_key="k1",
        )
        rec = build_recommendation(settings, event, client=object())
        assert rec.action == "SKIP"
        assert REASON_AUTHORITATIVE_EVENT_REQUIRED in rec.reason_codes

    def test_stale_authoritative_event_skips(self, tmp_path, monkeypatch):
        settings = make_test_settings(
            database_path=str(tmp_path / "rec2.db"),
            copy_max_trade_age_min=30,
            copy_max_event_age_seconds=600,
            copy_min_resolved_trades=5,
        )
        initialize_database(settings)
        monkeypatch.setattr(
            "app.copy_intel.recommendation.compute_trader_metrics",
            lambda *a, **k: type("M", (), {"eligible": True, "ineligible_reason": None})(),
        )
        monkeypatch.setattr("app.copy_intel.recommendation.score_trader", lambda m: 80.0)
        now = 1_700_000_000
        event = TradeEvent(
            wallet="0xee00ba338c59557141789b127927a55f5cc5cea1",
            event_type="open",
            condition_id="0xabc",
            asset="token",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=100.0,
            tx_hash="0x1",
            ts=now - 10_000,
            title="Test market",
            slug="test-market",
            dedup_key="k1",
            event_uid="ff1:" + ("11" * 32),
            trader_vwap=0.5,
            first_fill_ts=now - 10_000,
            last_fill_ts=now - 10_000,
            matched_fill_count=1,
            detected_at=now,
        )
        rec = build_recommendation(settings, event, client=object(), now=float(now))
        assert rec.action == "SKIP"
        assert REASON_EVENT_TOO_OLD in rec.reason_codes
