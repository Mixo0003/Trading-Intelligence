"""Tests for trader discovery, scoring, and settings (V2 Phase 2)."""

from __future__ import annotations

import pytest

from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.discovery import dedupe_candidates, discover_candidates, filter_scores
from app.copy_intel.discovery_metrics import compute_discovery_metrics
from app.copy_intel.discovery_scoring import score_discovery_trader
from app.copy_intel.models import DiscoveryCandidate, DiscoveryMetrics
from app.copy_intel.settings_profiles import (
    build_settings,
    get_user_settings,
    set_profile,
    update_setting,
)
from app.database.storage import initialize_database
from app.telegram.copy_intel.formatting import format_settings_card, format_trader_card
from app.copy_intel.watchlist import add_wallet, list_entries
from tests.conftest import make_test_settings


def _metrics(**overrides) -> DiscoveryMetrics:
    base = dict(
        wallet="0xee00ba338c59557141789b127927a55f5cc5cea1",
        label="S-Works",
        realized_pnl=10000.0,
        roi=0.15,
        resolved_count=40,
        active_days=90,
        win_rate=0.58,
        avg_position_size=200.0,
        median_position_size=150.0,
        max_drawdown=0.20,
        recent_pnl_30d=1200.0,
        lifetime_pnl=10000.0,
        main_categories=("sports", "politics"),
        category_concentration=0.30,
        market_concentration=0.15,
        single_win_contribution=0.18,
        liquidity_quality=0.7,
        wash_score=0.05,
        incomplete_metrics=(),
        profile_url="https://polymarket.com/profile/0xee00ba338c59557141789b127927a55f5cc5cea1",
    )
    base.update(overrides)
    return DiscoveryMetrics(**base)


class TestDiscoveryPaginationAndDedupe:
    def test_leaderboard_pages_paginate(self, monkeypatch):
        from app.copy_intel.api_result import ApiSuccess

        settings = make_test_settings()
        calls = []

        def fake_fetch(self, *, limit=25, offset=0, time_period=None, order_by=None, category=None):
            calls.append(offset)
            if offset >= 50:
                return ApiSuccess(data=[])
            return ApiSuccess(
                data=[
                    {
                        "proxyWallet": f"0x{'%040d' % (offset + i)}",
                        "userName": f"u{offset + i}",
                        "pnl": 100 - offset,
                        "vol": 1000,
                    }
                    for i in range(limit)
                ]
            )

        monkeypatch.setattr(CopyDataClient, "fetch_leaderboard", fake_fetch)
        client = CopyDataClient(settings)
        result = client.fetch_leaderboard_pages(page_size=25, max_pages=3)
        assert isinstance(result, ApiSuccess)
        assert len(result.data) == 50
        assert calls == [0, 25, 50]

    def test_leaderboard_api_failure_not_empty_list(self, monkeypatch):
        from app.copy_intel.api_result import ApiFailure
        from app.copy_intel.discovery import DiscoveryError, discover_candidates

        settings = make_test_settings()

        def fail_fetch(self, **kwargs):
            return ApiFailure(
                code="leaderboard_unavailable",
                status_code=None,
                retryable=True,
                request_count=1,
                message="down",
            )

        monkeypatch.setattr(CopyDataClient, "fetch_leaderboard", fail_fetch)
        with pytest.raises(DiscoveryError) as exc:
            discover_candidates(settings, client=CopyDataClient(settings))
        assert exc.value.code == "api_unavailable"

    def test_wallet_profile_deduplication(self):
        a = DiscoveryCandidate(
            wallet="0xEe00ba338c59557141789b127927a55f5cc5cea1",
            label="A",
            sources=("active",),
            leaderboard_pnl=10,
            leaderboard_vol=100,
            category=None,
        )
        b = DiscoveryCandidate(
            wallet="0xee00ba338c59557141789b127927a55f5cc5cea1",
            label=None,
            sources=("profitable",),
            leaderboard_pnl=20,
            leaderboard_vol=50,
            category="Sports",
        )
        merged = dedupe_candidates([a, b])
        assert len(merged) == 1
        assert set(merged[0].sources) == {"active", "profitable"}
        assert merged[0].leaderboard_pnl == 20


class TestDiscoveryMetricsAndPenalties:
    def test_metric_calculation_from_client(self):
        settings = make_test_settings()

        class FakeClient:
            def fetch_closed_positions(self, wallet, limit=200):
                return [
                    {"realizedPnl": 100, "totalBought": 200, "conditionId": "c1", "eventSlug": "sports-x"},
                    {"realizedPnl": -20, "totalBought": 100, "conditionId": "c2", "eventSlug": "sports-y"},
                    {"realizedPnl": 50, "totalBought": 100, "conditionId": "c3", "eventSlug": "politics-z"},
                ]

            def fetch_trades(self, wallet, limit=200):
                return [
                    {"side": "BUY", "size": 10, "price": 0.5, "timestamp": 1_700_000_000, "conditionId": "c1"},
                    {"side": "SELL", "size": 5, "price": 0.6, "timestamp": 1_700_100_000, "conditionId": "c2"},
                ]

            def fetch_activity(self, wallet, limit=200):
                return [{"type": "TRADE", "timestamp": 1_700_050_000, "usdcSize": 5, "side": "BUY"}]

            def fetch_positions(self, wallet, limit=100):
                return [{"currentValue": 40, "conditionId": "c1", "eventSlug": "sports-x"}]

        cand = DiscoveryCandidate(
            wallet="0xee00ba338c59557141789b127927a55f5cc5cea1",
            label="T",
            sources=("active",),
            leaderboard_pnl=None,
            leaderboard_vol=None,
            category=None,
        )
        metrics = compute_discovery_metrics(cand, FakeClient())  # type: ignore[arg-type]
        assert metrics.resolved_count == 3
        assert metrics.realized_pnl == 130.0
        assert metrics.roi is not None
        assert metrics.win_rate == 2 / 3

    def test_one_hit_wonder_penalty(self):
        user = build_settings(1, "Balanced")
        good = score_discovery_trader(_metrics(single_win_contribution=0.15), user)
        bad = score_discovery_trader(_metrics(single_win_contribution=0.90), user)
        assert bad.performance_score < good.performance_score
        assert bad.final_score < good.final_score

    def test_drawdown_penalty(self):
        user = build_settings(1, "Balanced")
        low = score_discovery_trader(_metrics(max_drawdown=0.10), user)
        high = score_discovery_trader(_metrics(max_drawdown=0.70), user)
        assert high.risk_score < low.risk_score

    def test_concentration_penalty(self):
        user = build_settings(1, "Balanced")
        diversified = score_discovery_trader(_metrics(category_concentration=0.25), user)
        concentrated = score_discovery_trader(_metrics(category_concentration=0.85), user)
        assert concentrated.risk_score < diversified.risk_score

    def test_low_liquidity_penalty(self):
        user = build_settings(1, "Balanced")
        good = score_discovery_trader(_metrics(liquidity_quality=0.8), user)
        poor = score_discovery_trader(_metrics(liquidity_quality=0.2), user)
        assert poor.risk_score < good.risk_score

    def test_insufficient_data_handling(self):
        user = build_settings(1, "Balanced")
        score = score_discovery_trader(
            _metrics(
                resolved_count=1,
                active_days=1,
                realized_pnl=None,
                roi=None,
                win_rate=None,
                incomplete_metrics=("realized_pnl", "roi", "win_rate"),
            ),
            user,
        )
        assert score.rejected is True or score.decision == "AVOID"
        assert score.data_confidence == "Low"


class TestDiscoveryDecisions:
    def test_follow_watch_avoid_thresholds(self):
        user = build_settings(1, "Balanced")
        follow = score_discovery_trader(_metrics(), user)
        watch = score_discovery_trader(
            _metrics(resolved_count=12, active_days=20, win_rate=0.52, max_drawdown=0.35),
            user,
        )
        avoid = score_discovery_trader(
            _metrics(
                resolved_count=2,
                active_days=2,
                wash_score=0.5,
                incomplete_metrics=("realized_pnl",),
                realized_pnl=None,
            ),
            user,
        )
        assert follow.decision in {"FOLLOW", "WATCH"}
        assert watch.decision in {"WATCH", "AVOID", "FOLLOW"}
        assert avoid.decision == "AVOID"

    def test_score_stability(self):
        user = build_settings(1, "Balanced")
        m = _metrics()
        a = score_discovery_trader(m, user)
        b = score_discovery_trader(m, user)
        assert a.final_score == b.final_score
        assert a.performance_score == b.performance_score
        assert a.risk_score == b.risk_score


class TestSettingsPersistence:
    def test_default_balanced_and_persist(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "s.db"))
        initialize_database(settings)
        user = get_user_settings(settings, 42)
        assert user.profile == "Balanced"
        set_profile(settings, 42, "Conservative")
        again = get_user_settings(settings, 42)
        assert again.profile == "Conservative"
        assert again.min_resolved_markets == 40
        updated = update_setting(settings, 42, "alert_decisions", "FOLLOW_ONLY")
        assert updated.alert_decisions == "FOLLOW_ONLY"
        reloaded = get_user_settings(settings, 42)
        assert reloaded.alert_decisions == "FOLLOW_ONLY"


class TestTelegramDiscoveryCards:
    def test_trader_card_and_settings_card(self):
        user = build_settings(1, "Balanced")
        score = score_discovery_trader(_metrics(), user)
        card = format_trader_card(score)
        assert "Decision:" in card
        assert "Confidence:" in card
        assert "forecast" not in card.lower()
        assert "temperature" not in card.lower()
        assert "/copy_add" not in card
        settings_text = format_settings_card(user)
        assert "Balanced" in settings_text
        assert "TELEGRAM_TOKEN" not in settings_text

    def test_add_discovered_trader_to_watchlist(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "w.db"))
        initialize_database(settings)
        wallet = add_wallet(
            settings,
            "0xee00ba338c59557141789b127927a55f5cc5cea1",
            label="S-Works",
            added_by=1,
        )
        entries = list_entries(settings)
        assert len(entries) == 1
        assert entries[0].wallet == wallet
        assert entries[0].label == "S-Works"


class TestDiscoverCandidatesMocked:
    def test_discover_merges_sources(self, monkeypatch):
        settings = make_test_settings()

        def fake_pages(self, *, page_size=25, max_pages=3, time_period=None, order_by=None, category=None):
            wallet = "0xee00ba338c59557141789b127927a55f5cc5cea1"
            return [{"proxyWallet": wallet, "userName": "S-Works", "pnl": 100, "vol": 1000}]

        monkeypatch.setattr(CopyDataClient, "fetch_leaderboard_pages", fake_pages)
        cands = discover_candidates(settings, page_size=5, max_pages=1)
        assert len(cands) == 1
        assert len(cands[0].sources) >= 2


class TestFilterScores:
    def test_filter_high_risk(self):
        user = build_settings(1, "Balanced")
        scores = [
            score_discovery_trader(_metrics(), user),
            score_discovery_trader(_metrics(max_drawdown=0.9, wash_score=0.4, resolved_count=2), user),
        ]
        risky = filter_scores(scores, high_risk_only=True)
        assert all(s.risk_level == "High" or s.decision == "AVOID" for s in risky)
