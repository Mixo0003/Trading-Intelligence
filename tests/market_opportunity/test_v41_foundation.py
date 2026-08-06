"""Focused tests for Market Opportunity Intelligence V4.1 foundation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.market_opportunity_storage import (
    MIGRATION_ID,
    initialize_market_opportunity_schema,
    is_migration_applied,
    list_published_opportunity_uids,
)
from app.database.storage import get_connection, initialize_database
from app.health.monitor import probe_market_opportunity
from app.market_opportunity.detectors.registry import DetectorRegistry, default_registry
from app.market_opportunity.freshness import FreshnessError, assert_fresh
from app.market_opportunity.lifecycle import LifecycleError, assert_transition, can_transition
from app.market_opportunity.models import (
    AssetClass,
    ConfidenceBand,
    EvidenceRef,
    InstrumentRef,
    Opportunity,
    OpportunityFamily,
    OpportunitySide,
    OpportunityStatus,
    PriceLevel,
    Provenance,
)
from app.market_opportunity.paper import MarketOpportunitySafetyError, assert_paper_only
from app.market_opportunity.scheduler import (
    MOI_JOBS_REGISTERED_KEY,
    schedule_market_opportunity_jobs,
)
from app.market_opportunity.validation import ValidationError, validate_opportunity
from app.storage.retention_policies import build_retention_policies
from app.telegram.auth import ACCESS_DENIED
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.inline_menus import (
    CB_CRYPTO_MENU,
    CB_MOI_MENU,
    CB_MOI_TOP,
    CB_OPP,
    main_inline_keyboard,
    moi_hub_keyboard,
    polymarket_inline_keyboard,
)
from app.telegram.market_opportunity.formatting import TOP_EMPTY_TEXT
from tests.conftest import make_test_settings


def _now() -> datetime:
    return datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _prov(*, age_seconds: int = 10, series_kind: str = "quote") -> Provenance:
    fetched = _now() - timedelta(seconds=age_seconds)
    return Provenance(
        source_id="test_source",
        fetched_at=fetched,
        source_event_at=fetched,
        series_kind=series_kind,
    )


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        asset_class=AssetClass.COMMODITY,
        symbol="GC",
        source_id="test_feed",
        venue="COMEX",
        market_id="COMEX:GCZ6",
        display_name="Gold futures front",
    )


def _level(label: str, price: str, *, age_seconds: int = 10) -> PriceLevel:
    return PriceLevel(
        label=label,
        price=Decimal(price),
        provenance=_prov(age_seconds=age_seconds),
    )


def _opportunity(**overrides) -> Opportunity:
    base = dict(
        opportunity_uid="moi_test_1",
        family=OpportunityFamily.TREND_MOMENTUM,
        opportunity_type="trend_example",
        side=OpportunitySide.LONG,
        instrument=_instrument(),
        status=OpportunityStatus.VALIDATED,
        confidence=ConfidenceBand.MEDIUM,
        verified_price=_level("mid", "2400"),
        entry_zone_low=Decimal("2390"),
        entry_zone_high=Decimal("2410"),
        targets=(_level("t1", "2500"),),
        invalidation=_level("stop", "2300"),
        time_horizon="5d",
        reward_risk=Decimal("2.0"),
        evidence=(
            EvidenceRef(
                evidence_id="e1",
                summary="Synthetic test evidence",
                provenance=_prov(),
                is_catalyst=True,
            ),
        ),
        counterarguments=("Synthetic risk for tests",),
        paper_only=True,
        created_at=_now(),
        updated_at=_now(),
        expires_at=_now() + timedelta(days=5),
    )
    base.update(overrides)
    return Opportunity(**base)


class TestModels:
    def test_paper_only_required(self):
        with pytest.raises(ValueError, match="paper_only"):
            _opportunity(paper_only=False)

    def test_ambiguous_gold_rejected(self):
        with pytest.raises(ValueError, match="ambiguous"):
            InstrumentRef(
                asset_class=AssetClass.COMMODITY,
                symbol="Gold",
                source_id="x",
                venue="y",
                market_id="Gold",
            )

    def test_confidence_band_only(self):
        opp = _opportunity(confidence=ConfidenceBand.HIGH)
        assert opp.confidence is ConfidenceBand.HIGH


class TestLifecycle:
    def test_allowed_and_forbidden(self):
        assert can_transition(
            OpportunityStatus.DISCOVERED, OpportunityStatus.VALIDATED
        )
        assert not can_transition(
            OpportunityStatus.DISCOVERED, OpportunityStatus.PUBLISHED
        )
        with pytest.raises(LifecycleError):
            assert_transition(
                OpportunityStatus.CALIBRATED, OpportunityStatus.DISCOVERED
            )


class TestValidationFreshness:
    def test_valid_opportunity_passes(self):
        validate_opportunity(_opportunity(), now=_now())

    def test_stale_price_rejected(self):
        with pytest.raises(ValidationError, match="stale"):
            validate_opportunity(
                _opportunity(verified_price=_level("mid", "2400", age_seconds=10_000)),
                now=_now(),
            )

    def test_freshness_helper(self):
        with pytest.raises(FreshnessError):
            assert_fresh(_prov(age_seconds=10_000), now=_now())

    def test_invented_confidence_percent_rejected(self):
        from app.market_opportunity.validation import assert_no_invented_confidence_percent

        with pytest.raises(ValidationError):
            assert_no_invented_confidence_percent(87.5)


class TestPaperGuard:
    def test_read_only_violation(self):
        settings = make_test_settings(market_opportunity_read_only=False)
        with pytest.raises(MarketOpportunitySafetyError):
            assert_paper_only(settings)

    def test_auto_alerts_blocked(self):
        settings = make_test_settings(market_opportunity_auto_alerts_enabled=True)
        with pytest.raises(MarketOpportunitySafetyError):
            assert_paper_only(settings)


class TestDetectorRegistry:
    def test_default_empty_enabled(self):
        assert default_registry.list_enabled() == ()
        assert "gold" not in default_registry.list_registered()
        assert "wti" not in default_registry.list_registered()

    def test_register_disabled_by_default(self):
        class _Stub:
            detector_id = "stub_test"
            family = OpportunityFamily.BREAKOUT

            def detect(self, context):
                raise AssertionError("must not be called")

        reg = DetectorRegistry()
        reg.register(_Stub(), enabled=False)
        assert reg.list_enabled() == ()
        assert reg.list_registered() == ("stub_test",)


class TestMigrationTempDb:
    def test_apply_and_idempotent(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "moi_v1.db"))
        initialize_database(settings)
        assert is_migration_applied(settings)
        initialize_market_opportunity_schema(settings)
        initialize_market_opportunity_schema(settings)
        with get_connection(settings) as connection:
            markers = connection.execute(
                "SELECT migration_id FROM moi_schema_migrations"
            ).fetchall()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert [row[0] for row in markers] == [MIGRATION_ID]
        assert {
            "moi_schema_migrations",
            "moi_opportunities",
            "moi_evidence",
            "moi_outcomes",
            "moi_runtime_status",
        }.issubset(tables)
        assert list_published_opportunity_uids(settings) == []

    def test_retention_policies_registered(self):
        names = {p.dataset for p in build_retention_policies()}
        assert "moi_opportunities" in names
        assert "moi_evidence" in names
        assert "moi_outcomes" in names
        assert "moi_runtime_status" in names
        for policy in build_retention_policies():
            if policy.dataset.startswith("moi_"):
                assert policy.kind == "PROTECTED"
                assert policy.batch_size == 0


class TestSchedulerHealth:
    def test_disabled_scheduler_registers_nothing(self):
        settings = make_test_settings(market_opportunity_enabled=False)
        app = MagicMock()
        app.bot_data = {}
        app.job_queue = MagicMock()
        schedule_market_opportunity_jobs(app, settings)
        app.job_queue.run_repeating.assert_not_called()
        assert MOI_JOBS_REGISTERED_KEY not in app.bot_data

    def test_health_disabled_not_failure(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "health.db"),
            market_opportunity_enabled=False,
        )
        initialize_database(settings)
        status = probe_market_opportunity(settings)
        assert status.status == "DISABLED"
        assert status.component == "market_opportunity"


def _callback_update(data: str, *, user_id: int):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = data
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=user_id, username="u", first_name="T")
    update.message = None
    return update


class TestTelegramAdminGate:
    @pytest.mark.asyncio
    async def test_admin_opens_hub(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "tg.db"), admin_ids=[1]
        )
        context = MagicMock()
        context.application.bot_data = {"settings": settings}
        update = _callback_update(CB_MOI_MENU, user_id=1)
        await inline_callback_handler(update, context)
        update.callback_query.message.edit_text.assert_awaited()
        args, kwargs = update.callback_query.message.edit_text.await_args
        assert "Market Opportunities" in args[0]
        assert kwargs.get("reply_markup") is not None

    @pytest.mark.asyncio
    async def test_non_admin_denied(self, tmp_path):
        settings = make_test_settings(
            database_path=str(tmp_path / "tg2.db"), admin_ids=[1]
        )
        context = MagicMock()
        context.application.bot_data = {"settings": settings}
        update = _callback_update(CB_MOI_TOP, user_id=99)
        await inline_callback_handler(update, context)
        update.effective_message.reply_text.assert_awaited_with(ACCESS_DENIED)

    def test_menu_visibility_and_polymarket_rename(self):
        guest = [
            btn.text
            for row in main_inline_keyboard(is_admin=False).inline_keyboard
            for btn in row
        ]
        admin = [
            btn.text
            for row in main_inline_keyboard(is_admin=True).inline_keyboard
            for btn in row
        ]
        assert "📊 Market Opportunities" not in guest
        assert "📊 Market Opportunities" in admin
        pm_labels = [
            btn.text
            for row in polymarket_inline_keyboard().inline_keyboard
            for btn in row
        ]
        assert "🎯 Opportunity Scanner" in pm_labels
        assert "🎯 Market Opportunities" not in pm_labels
        assert CB_OPP in {
            btn.callback_data
            for row in polymarket_inline_keyboard().inline_keyboard
            for btn in row
        }

    def test_namespace_isolation(self):
        moi = {
            btn.callback_data
            for row in moi_hub_keyboard().inline_keyboard
            for btn in row
            if btn.callback_data
        }
        assert all(d.startswith("moi:") or d.startswith("nav:") for d in moi)
        assert CB_MOI_MENU.startswith("moi:")
        assert CB_OPP.startswith("nav:") or CB_OPP == "nav:opp"
        assert CB_CRYPTO_MENU.startswith("crypto:")
        assert TOP_EMPTY_TEXT.startswith("📊 Top Opportunities")


class TestNoTradingImports:
    def test_market_opportunity_package_has_no_ccxt_or_order_apis(self):
        import app.market_opportunity as pkg
        import pathlib

        root = pathlib.Path(pkg.__file__).parent
        banned = ("ccxt", "create_order", "place_order", "withdraw", "private_key")
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                assert token not in text, f"{path} contains {token}"
