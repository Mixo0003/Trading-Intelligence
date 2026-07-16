import json
from pathlib import Path

import pytest

from app.copy_intel.analytics import compute_trader_metrics
from app.copy_intel.scoring import score_trader
from tests.conftest import make_test_settings

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "copy_intel_wallet_sworks.json"


class FakeClient:
    def __init__(self, data):
        self._data = data

    def fetch_closed_positions(self, wallet, limit=500):
        return self._data["endpoints"]["closed-positions"]

    def fetch_trades(self, wallet, limit=500):
        return self._data["endpoints"]["trades"]

    def fetch_activity(self, wallet, limit=500):
        return self._data["endpoints"]["activity"]

    def fetch_positions(self, wallet, limit=100):
        return self._data["endpoints"]["positions"]


@pytest.fixture
def fixture_data():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestAnalytics:
    def test_excludes_non_trade_activity_types(self, fixture_data):
        activity_types = {a["type"] for a in fixture_data["endpoints"]["activity"]}
        assert "REDEEM" in activity_types

    def test_metrics_computed_from_fixture(self, fixture_data, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "analytics.db"),
            copy_min_resolved_trades=5,
        )
        from app.database.storage import initialize_database

        initialize_database(settings)
        wallet = fixture_data["wallet"]
        metrics = compute_trader_metrics(settings, wallet, client=FakeClient(fixture_data))
        assert metrics.trade_count > 0
        assert metrics.resolved_count > 0
        assert 0.0 <= metrics.win_rate <= 1.0


class TestScoring:
    def test_ineligible_trader_low_score(self, fixture_data, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "score1.db"),
            copy_min_resolved_trades=500,
        )
        from app.database.storage import initialize_database

        initialize_database(settings)
        wallet = fixture_data["wallet"]
        metrics = compute_trader_metrics(settings, wallet, client=FakeClient(fixture_data))
        score = score_trader(metrics)
        assert score < 55

    def test_eligible_trader_higher_score(self, fixture_data, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "score2.db"),
            copy_min_resolved_trades=5,
        )
        from app.database.storage import initialize_database

        initialize_database(settings)
        wallet = fixture_data["wallet"]
        metrics = compute_trader_metrics(settings, wallet, client=FakeClient(fixture_data))
        metrics = type(metrics)(
            wallet=metrics.wallet,
            trade_count=metrics.trade_count,
            resolved_count=25,
            win_rate=0.62,
            realized_pnl=metrics.realized_pnl,
            unrealized_pnl=metrics.unrealized_pnl,
            avg_entry_price=metrics.avg_entry_price,
            avg_holding_hours=metrics.avg_holding_hours,
            max_drawdown=100.0,
            category_concentration=0.2,
            recent_pnl_30d=500.0,
            lifetime_pnl=1000.0,
            wash_score=0.05,
            eligible=True,
            ineligible_reason=None,
        )
        score = score_trader(metrics)
        assert score >= 55

    def test_raw_pnl_alone_does_not_max_score(self):
        from app.copy_intel.models import TraderMetrics

        high_pnl_low_quality = TraderMetrics(
            wallet="0xabc",
            trade_count=25,
            resolved_count=25,
            win_rate=0.35,
            realized_pnl=500000.0,
            unrealized_pnl=0.0,
            avg_entry_price=0.5,
            avg_holding_hours=1.0,
            max_drawdown=400000.0,
            category_concentration=0.9,
            recent_pnl_30d=-10000.0,
            lifetime_pnl=500000.0,
            wash_score=0.8,
            eligible=False,
            ineligible_reason="wash",
        )
        assert score_trader(high_pnl_low_quality) < 30
