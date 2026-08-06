"""Focused offline tests for Market Opportunity Intelligence V4.2 WTI."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.market_opportunity_storage import (
    MIGRATION_ID,
    get_opportunity,
    initialize_market_opportunity_schema,
    is_migration_applied,
    list_opportunities,
    list_published_opportunity_uids,
)
from app.database.storage import initialize_database
from app.health.monitor import probe_market_opportunity
from app.market_opportunity.blackouts import (
    BlackoutError,
    assert_schedule_coverage,
    is_in_blackout,
)
from app.market_opportunity.detectors.base import (
    WTI_BREAKOUT_DETECTOR_ID,
    WTI_INVENTORY_DETECTOR_ID,
    WTI_TREND_DETECTOR_ID,
    WtiDetectorContext,
)
from app.market_opportunity.detectors.registry import (
    DetectorRegistry,
    default_registry,
    register_wti_detectors,
)
from app.market_opportunity.detectors.wti_breakout import WtiBreakoutDetector
from app.market_opportunity.detectors.wti_inventory import WtiInventoryDetector
from app.market_opportunity.detectors.wti_trend import WtiTrendDetector
from app.market_opportunity.freshness import FreshnessError, assert_fresh
from app.market_opportunity.geometry import GeometryError, build_geometry
from app.market_opportunity.indicators import atr14, inventory_wow_zscore, sma
from app.market_opportunity.models import (
    OpportunitySide,
    Provenance,
)
from app.market_opportunity.outcomes import MeasurableOpportunity, measure_opportunity
from app.market_opportunity.pipeline import (
    WtiPipelineInput,
    apply_inventory_confluence,
    fred_conflict,
    resolve_confidence,
    run_stage_a,
    run_wti_scan,
)
from app.market_opportunity.scheduler import (
    MOI_JOBS_REGISTERED_KEY,
    compute_wti_data_hash,
    data_hash_changed,
    schedule_market_opportunity_jobs,
)
from app.market_opportunity.sources.eia import (
    EIA_WTI_INVENTORY_FREQUENCY,
    EIA_WTI_INVENTORY_PROCESS_NAME,
    EIA_WTI_INVENTORY_SERIES_ID,
    EIA_WTI_INVENTORY_TITLE,
    EIA_WTI_INVENTORY_UNITS,
    DailyBar,
    InventoryPoint,
    parse_inventory_wcestus1,
    parse_rwtc_daily,
)
from app.market_opportunity.sources.fred import FredObservation
from app.telegram.auth import ACCESS_DENIED
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.inline_menus import CB_MOI_GOLD, CB_MOI_WTI, moi_hub_keyboard
from app.telegram.market_opportunity.formatting import GOLD_STUB_TEXT, HUB_TEXT
from tests.conftest import make_test_settings

FIXTURES = Path(__file__).parent / "fixtures"


def _bar(day: date, value: float | str) -> DailyBar:
    px = Decimal(str(value))
    return DailyBar(
        period=day, open=px, high=px, low=px, close=px, value=px
    )


def _uptick_bars(n: int = 70, start: date = date(2026, 4, 1)) -> list[DailyBar]:
    """Rising series ending with clear SMA stack for trend LONG."""
    bars: list[DailyBar] = []
    day = start
    price = Decimal("70")
    for i in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        # Accelerate near the end so close >> SMA20 > SMA50.
        step = Decimal("0.15") if i < n - 10 else Decimal("1.2")
        price = price + step
        bars.append(_bar(day, price))
        day += timedelta(days=1)
    return bars


def _inventory_draw(n: int = 30) -> list[InventoryPoint]:
    """Mostly flat then sharp draw → positive oil surprise (LONG)."""
    points: list[InventoryPoint] = []
    # End near 2026-07-31 so as-of age is fresh vs _now()=2026-08-05.
    end = date(2026, 7, 31)
    day = end - timedelta(days=7 * (n - 1))
    level = Decimal("420000")
    for i in range(n):
        if i == n - 1:
            level = level - Decimal("25000")
        elif i == n - 2:
            level = level - Decimal("500")
        points.append(InventoryPoint(period=day, value=level))
        day += timedelta(days=7)
    return points


def _now() -> datetime:
    # Wednesday 2026-08-05 — outside WPSR window in checked-in calendar.
    return datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def _prov_official(
    *,
    kind: str,
    as_of: datetime,
    fetched: datetime | None = None,
) -> Provenance:
    return Provenance(
        source_id="EIA",
        fetched_at=fetched or _now(),
        source_event_at=as_of,
        series_kind=kind,
    )


class TestEiaMetadataAndParsing:
    def test_inventory_series_frozen_metadata(self):
        assert EIA_WTI_INVENTORY_SERIES_ID == "WCESTUS1"
        assert "excluding SPR" in EIA_WTI_INVENTORY_TITLE
        assert EIA_WTI_INVENTORY_FREQUENCY == "weekly"
        assert EIA_WTI_INVENTORY_UNITS == "MBBL"
        assert EIA_WTI_INVENTORY_PROCESS_NAME == "Ending Stocks Excluding SPR"

    def test_parse_rwtc_fixture(self):
        payload = {
            "response": {
                "data": [
                    {
                        "period": "2026-08-03",
                        "series": "RWTC",
                        "value": "81.96",
                        "units": "$/BBL",
                        "series-description": (
                            "Cushing, OK WTI Spot Price FOB (Dollars per Barrel)"
                        ),
                    },
                    {
                        "period": "2026-07-31",
                        "series": "RWTC",
                        "value": "86.16",
                        "units": "$/BBL",
                    },
                ]
            }
        }
        bars = parse_rwtc_daily(payload)
        assert bars[0].period == date(2026, 7, 31)
        assert bars[-1].close == Decimal("81.96")
        assert bars[-1].high == bars[-1].low == bars[-1].close

    def test_parse_inventory_fixture(self):
        payload = {
            "response": {
                "data": [
                    {
                        "period": "2026-07-31",
                        "series": "WCESTUS1",
                        "value": "406987",
                        "units": "MBBL",
                        "process-name": "Ending Stocks Excluding SPR",
                        "series-description": (
                            "U.S. Ending Stocks excluding SPR of Crude Oil "
                            "(Thousand Barrels)"
                        ),
                    }
                ]
            }
        }
        points = parse_inventory_wcestus1(payload)
        assert points[0].value == Decimal("406987")


_GEOM_TOL = Decimal("1e-12")


def _assert_targets_match_r(geom) -> None:
    """T1=1.2R and T2=1.5R within defined Decimal tolerance."""
    assert abs(abs(geom.t1 - geom.entry_mid) - Decimal("1.2") * geom.r) <= _GEOM_TOL
    assert abs(abs(geom.t2 - geom.entry_mid) - Decimal("1.5") * geom.r) <= _GEOM_TOL


def _downtick_bars(n: int = 70, start: date = date(2026, 4, 1)) -> list[DailyBar]:
    """Falling series for SHORT trend geometry."""
    bars: list[DailyBar] = []
    day = start
    price = Decimal("120")
    for i in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        step = Decimal("0.15") if i < n - 10 else Decimal("1.2")
        price = price - step
        bars.append(_bar(day, price))
        day += timedelta(days=1)
    return bars


class TestIndicatorsGeometry:
    def test_atr14_close_to_close(self):
        bars = [_bar(date(2026, 1, 1) + timedelta(days=i), 100 + i) for i in range(20)]
        assert atr14(bars) == Decimal("1")

    def test_trend_geometry_long_proofs(self):
        bars = _uptick_bars()
        geom = build_geometry(bars, side=OpportunitySide.LONG, structure="trend")
        assert geom.r > 0
        assert geom.entry_low <= geom.entry_high
        assert geom.invalidation < geom.entry_low
        assert geom.entry_low <= geom.entry_mid <= geom.entry_high < geom.t1 <= geom.t2
        _assert_targets_match_r(geom)

    def test_trend_geometry_short_proofs(self):
        bars = _downtick_bars()
        geom = build_geometry(bars, side=OpportunitySide.SHORT, structure="trend")
        assert geom.r > 0
        assert geom.entry_low <= geom.entry_high
        assert geom.invalidation > geom.entry_high
        assert geom.t2 <= geom.t1 < geom.entry_low <= geom.entry_mid <= geom.entry_high
        _assert_targets_match_r(geom)

    def test_breakout_clip_stays_valid_side_of_invalidation(self):
        bars = _uptick_bars()
        last = bars[-1]
        bump = last.close + Decimal("5")
        bars = list(bars[:-1]) + [
            DailyBar(
                period=last.period,
                open=bump,
                high=bump,
                low=bump,
                close=bump,
                value=bump,
            )
        ]
        geom = build_geometry(bars, side=OpportunitySide.LONG, structure="breakout")
        assert geom.r > 0
        assert geom.invalidation < geom.entry_low
        assert geom.entry_low <= geom.entry_high
        # Clipped entry must not cross invalidation.
        assert geom.entry_low >= geom.invalidation + Decimal("0.05") * geom.atr
        _assert_targets_match_r(geom)

    def test_malformed_geometry_fails_closed(self):
        with pytest.raises(GeometryError):
            build_geometry([], side=OpportunitySide.LONG, structure="trend")
        short_bars = _uptick_bars(n=10)
        with pytest.raises(GeometryError):
            build_geometry(short_bars, side=OpportunitySide.LONG, structure="trend")
        # Flat series → ATR=0 → fail closed.
        flat = [_bar(date(2026, 1, 1) + timedelta(days=i), "80") for i in range(30)]
        with pytest.raises(GeometryError):
            build_geometry(flat, side=OpportunitySide.LONG, structure="trend")


class TestDetectorsAndConfluence:
    def test_trend_long_predicate(self):
        bars = _uptick_bars()
        cands = WtiTrendDetector().detect(WtiDetectorContext(bars=bars))
        assert any(c.side is OpportunitySide.LONG for c in cands)

    def test_cold_start_suppresses(self):
        bars = _uptick_bars(n=20)
        assert WtiTrendDetector().detect(WtiDetectorContext(bars=bars)) == []

    def test_inventory_alone_dropped(self):
        bars = _uptick_bars()
        # Flat price → no trend/breakout; inventory candidate alone.
        flat = [_bar(b.period, "80") for b in bars]
        inv = _inventory_draw()
        stage_a = run_stage_a(WtiDetectorContext(bars=flat, inventory=inv))
        inv_only = [c for c in stage_a if c.detector_id == WTI_INVENTORY_DETECTOR_ID]
        # May or may not emit inventory depending on z; confluence must drop alone.
        merged = apply_inventory_confluence(stage_a)
        assert all(c.detector_id != WTI_INVENTORY_DETECTOR_ID for c in merged) or any(
            c.detector_id in {WTI_TREND_DETECTOR_ID, WTI_BREAKOUT_DETECTOR_ID}
            for c in merged
        )
        if inv_only and not any(
            c.detector_id in {WTI_TREND_DETECTOR_ID, WTI_BREAKOUT_DETECTOR_ID}
            for c in stage_a
        ):
            assert all(c.detector_id != WTI_INVENTORY_DETECTOR_ID for c in merged)

    def test_inventory_confluence_keeps_with_trend(self):
        bars = _uptick_bars()
        inv = _inventory_draw()
        stage_a = run_stage_a(WtiDetectorContext(bars=bars, inventory=inv))
        merged = apply_inventory_confluence(stage_a)
        assert any(c.detector_id == WTI_TREND_DETECTOR_ID for c in merged)


class TestFreshnessFredConfidence:
    def test_eia_daily_spot_asof_not_tick(self):
        as_of = _now() - timedelta(days=2)
        assert_fresh(_prov_official(kind="eia_daily_spot", as_of=as_of), now=_now())
        stale = _prov_official(kind="eia_daily_spot", as_of=_now() - timedelta(days=4))
        with pytest.raises(FreshnessError):
            assert_fresh(stale, now=_now())

    def test_tick_quote_unchanged(self):
        old = Provenance(
            source_id="x",
            fetched_at=_now() - timedelta(seconds=200),
            source_event_at=_now() - timedelta(seconds=200),
            series_kind="quote",
        )
        with pytest.raises(FreshnessError):
            assert_fresh(old, now=_now())

    def test_fred_conflict_threshold(self):
        assert fred_conflict(Decimal("100"), Decimal("100.4")) is False
        assert fred_conflict(Decimal("100"), Decimal("101")) is True

    def test_confidence_caps_without_fred(self):
        band = resolve_confidence(
            has_structure=True,
            inventory_abs_z=Decimal("2.0"),
            fred_agree=False,
            fred_present=False,
        )
        from app.market_opportunity.models import ConfidenceBand

        assert band is ConfidenceBand.MEDIUM


class TestBlackouts:
    def test_wpsr_window(self):
        inside = datetime(2026, 8, 6, 14, 30, tzinfo=timezone.utc)
        assert is_in_blackout(inside) is True
        outside = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)
        assert is_in_blackout(outside) is False

    def test_missing_wpsr_thursday_fail_closed(self, tmp_path):
        # Schedule with required policy but no covering WPSR for target Thursday.
        payload = {
            "timezone_display": "America/New_York",
            "policy": {
                "WPSR": {"before_hours": 2, "after_hours": 2},
                "CPI": {"before_hours": 1, "after_hours": 2},
                "FOMC": {"before_hours": 2, "after_hours": 3},
            },
            "events": [
                {
                    "id": "cpi-only",
                    "kind": "CPI",
                    "start_utc": "2026-09-01T12:00:00+00:00",
                    "end_utc": "2026-09-01T15:00:00+00:00",
                }
            ],
        }
        path = tmp_path / "sparse.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        from app.market_opportunity.blackouts import clear_schedule_cache

        clear_schedule_cache()
        thursday = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)
        with pytest.raises(BlackoutError):
            assert_schedule_coverage(thursday, path=path)


class TestPipelineStorageOutcomes:
    def test_stage_a_no_persistence(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "a.db"))
        initialize_database(settings)
        bars = _uptick_bars()
        run_stage_a(WtiDetectorContext(bars=bars, inventory=_inventory_draw()))
        assert list_published_opportunity_uids(settings) == []

    def test_stage_b_round_trip(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "b.db"))
        initialize_database(settings)
        bars = _uptick_bars()
        # Align last bar period near _now for freshness.
        last = bars[-1]
        bars = list(bars[:-1]) + [
            DailyBar(
                period=date(2026, 8, 3),
                open=last.close,
                high=last.close,
                low=last.close,
                close=last.close,
                value=last.close,
            )
        ]
        fred = [
            FredObservation(period=date(2026, 8, 3), value=bars[-1].close),
        ]
        result = run_wti_scan(
            settings,
            WtiPipelineInput(
                bars=bars,
                inventory=_inventory_draw(),
                fred=fred,
                fetched_at=_now(),
                now=_now(),
            ),
            persist=True,
        )
        assert result.stage_a_count >= 1
        assert list_published_opportunity_uids(settings)
        uid = list_published_opportunity_uids(settings)[0]
        row = get_opportunity(settings, uid)
        assert row is not None
        assert row["symbol"] == "WTI_CUSHING"
        assert row["paper_only"] in (1, True)
        assert is_migration_applied(settings)
        # Still only v1 marker
        assert MIGRATION_ID == "market_opportunity_v1"

    def test_outcome_ambiguous_same_bar(self):
        opp = MeasurableOpportunity(
            opportunity_uid="wti:wti_trend:2026-08-03",
            side=OpportunitySide.LONG,
            entry_zone_low=Decimal("100"),
            entry_zone_high=Decimal("101"),
            targets=(Decimal("102"), Decimal("103")),
            invalidation=Decimal("99"),
            signal_period=date(2026, 8, 3),
        )
        # Same bar touches entry, target, and invalidation via equal OHLC mid mess —
        # use a bar that spans all levels when high=low=close can't; for RWTC
        # ambiguity requires constructing a synthetic wide bar.
        wide = DailyBar(
            period=date(2026, 8, 4),
            open=Decimal("100.5"),
            high=Decimal("103"),
            low=Decimal("98"),
            close=Decimal("100.5"),
            value=Decimal("100.5"),
        )
        signal = _bar(date(2026, 8, 3), "100.5")
        out = measure_opportunity(opp, [signal, wide], measured_at=_now())
        # Ambiguous path should not invent a clean win.
        assert out.entry_reached is True
        assert out.invalidation_reached is not True or out.targets_reached == ()


class TestSchedulerHealthRegistryTelegram:
    def test_hash_skip(self):
        bars = _uptick_bars(n=60)
        h1 = compute_wti_data_hash(bars, None)
        assert data_hash_changed(None, h1) is True
        assert data_hash_changed(h1, h1) is False

    def test_disabled_registers_nothing(self):
        settings = make_test_settings(market_opportunity_enabled=False)
        app = MagicMock()
        app.bot_data = {}
        app.job_queue = MagicMock()
        schedule_market_opportunity_jobs(app, settings)
        app.job_queue.run_repeating.assert_not_called()

    def test_enabled_registers_jobs_no_gold(self):
        settings = make_test_settings(market_opportunity_enabled=True)
        default_registry.clear()
        app = MagicMock()
        app.bot_data = {}
        app.job_queue = MagicMock()
        schedule_market_opportunity_jobs(app, settings)
        assert app.job_queue.run_repeating.call_count == 4
        assert MOI_JOBS_REGISTERED_KEY in app.bot_data
        ids = default_registry.list_registered()
        assert WTI_TREND_DETECTOR_ID in ids
        assert all("gold" not in i.lower() for i in ids)
        with pytest.raises(ValueError, match="Gold"):
            class _Gold:
                detector_id = "gold_trend"
                family = WtiTrendDetector().family

                def detect(self, context):
                    return []

            default_registry.register(_Gold())

    def test_health_disabled(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "h.db"),
            market_opportunity_enabled=False,
        )
        initialize_database(settings)
        status = probe_market_opportunity(settings)
        assert status.status == "DISABLED"

    def test_health_missing_fred_degraded(self, tmp_path):
        from app.market_opportunity.health import evaluate_moi_health

        settings = make_test_settings(
            database_path=str(tmp_path / "h2.db"),
            market_opportunity_enabled=True,
        )
        snap = evaluate_moi_health(
            settings,
            diagnostics={
                "wti_price": {"period": "2026-08-03"},
                "wti_inventory": {"period": "2026-07-31"},
                "fred_crosscheck": {"ok": False},
                "detector_runtime": {"ok": True, "published": 0, "suppressed": 0},
                "paper_outcome_runtime": {"ok": True, "measured": 0},
            },
            now=_now(),
        )
        assert snap["metrics"]["fred_crosscheck"]["state"] == "DEGRADED"
        assert snap["state"] in {"DEGRADED", "HEALTHY"}

    @pytest.mark.asyncio
    async def test_admin_wti_and_gold_stub(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "tg.db"), admin_ids=[1]
        )
        initialize_database(settings)
        context = MagicMock()
        context.application.bot_data = {"settings": settings}

        async def _run(data: str):
            update = MagicMock()
            update.callback_query = AsyncMock()
            update.callback_query.data = data
            update.callback_query.message = AsyncMock()
            update.callback_query.answer = AsyncMock()
            update.effective_message = update.callback_query.message
            update.effective_user = MagicMock(id=1, username="u", first_name="T")
            update.message = None
            await inline_callback_handler(update, context)
            return update

        u1 = await _run(CB_MOI_WTI)
        u1.callback_query.message.edit_text.assert_awaited()
        u2 = await _run(CB_MOI_GOLD)
        args, _ = u2.callback_query.message.edit_text.await_args
        assert "Deferred" in args[0] or "licensed" in args[0]

    def test_hub_mentions_wti_and_no_alerts(self):
        assert "WTI" in HUB_TEXT
        assert "No auto alerts" in HUB_TEXT
        assert "Gold" in GOLD_STUB_TEXT
        labels = [
            btn.callback_data
            for row in moi_hub_keyboard().inline_keyboard
            for btn in row
            if btn.callback_data
        ]
        assert CB_MOI_WTI in labels
        assert CB_MOI_GOLD in labels

    @pytest.mark.asyncio
    async def test_non_admin_denied(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "tg2.db"), admin_ids=[1]
        )
        context = MagicMock()
        context.application.bot_data = {"settings": settings}
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.data = CB_MOI_WTI
        update.callback_query.message = AsyncMock()
        update.callback_query.answer = AsyncMock()
        update.effective_message = AsyncMock()
        update.effective_user = MagicMock(id=99, username="x", first_name="N")
        update.message = None
        await inline_callback_handler(update, context)
        update.effective_message.reply_text.assert_awaited_with(ACCESS_DENIED)

    def test_register_wti_no_gold(self):
        reg = DetectorRegistry()
        ids = register_wti_detectors(reg, enabled=False)
        assert set(ids) == {
            WTI_TREND_DETECTOR_ID,
            WTI_BREAKOUT_DETECTOR_ID,
            WTI_INVENTORY_DETECTOR_ID,
        }
        assert reg.list_enabled() == ()
