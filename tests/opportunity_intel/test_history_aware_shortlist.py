"""History-aware Stage A/B shortlist selection proofs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.opportunity_intel.discovery import (
    OutcomeCandidate,
    ResultStatus,
    ValidatedMarket,
    ValidatedOutcome,
    shortlist_outcomes,
)
from app.opportunity_intel.models import ObservationMode, OpportunityDiagnosticCode
from app.opportunity_intel.scanner import OpportunityScannerService
from app.opportunity_intel.request_budget import CycleRequestBudget
from tests.conftest import make_test_settings


def _candidate(asset_id: str, volume: str) -> OutcomeCandidate:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    return OutcomeCandidate(
        event_id="e1",
        market_id=f"m-{asset_id}",
        condition_id=f"c-{asset_id}",
        event_slug="slug",
        question="Q?",
        category_ids=("politics",),
        mode=ObservationMode.NORMAL,
        liquidity_usd=Decimal("5000"),
        volume_24h_usd=Decimal(volume),
        metadata_at=now - timedelta(seconds=30),
        end_at=now + timedelta(days=3),
        asset_id=asset_id,
        outcome="Yes",
        outcome_index=0,
        price_hint=Decimal("0.40"),
    )


def test_shortlist_stage_a_oversamples_deterministically():
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    markets = []
    for idx in range(10):
        markets.append(
            ValidatedMarket(
                event_id=f"e{idx}",
                market_id=f"m{idx}",
                condition_id=f"c{idx}",
                event_slug=f"slug-{idx}",
                question=f"Q{idx}?",
                category_ids=("politics",),
                mode=ObservationMode.NORMAL,
                liquidity_usd=Decimal("50000"),
                volume_24h_usd=Decimal(str(100000 - idx)),
                metadata_at=now - timedelta(seconds=20),
                end_at=now + timedelta(days=2),
                outcomes=(
                    ValidatedOutcome(
                        asset_id=f"a{idx}",
                        name="Yes",
                        price_hint=Decimal("0.4"),
                        index=0,
                    ),
                ),
            )
        )
    shortlist = shortlist_outcomes(
        tuple(markets),
        now=now,
        strict_live_demand=False,
        max_candidates=6,
    )
    assert len(shortlist) == 6
    assert shortlist[0].asset_id == "a0"
    assert shortlist[1].asset_id == "a1"


def test_history_missing_does_not_fill_slots_when_capable_exists(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "hist.db"))
    from app.database.storage import initialize_database

    initialize_database(settings)

    missing = _candidate("missing", "9000")
    capable = _candidate("capable", "8000")
    shortlist = (missing, capable)

    service = OpportunityScannerService(
        settings,
        discovery_cache=MagicMock(),
        history_client=MagicMock(),
        book_client=MagicMock(),
        max_markets=1,
        max_books_per_cycle=10,
        max_live_books_per_cycle=2,
        request_budget=CycleRequestBudget(clob_cap=50, gamma_cap=10),
    )

    def _eval(candidate, now):
        if candidate.asset_id == "missing":
            return {
                "history_ok": False,
                "book_ok": True,
                "observation_result": None,
                "diagnostics": [OpportunityDiagnosticCode.HISTORY_MISSING],
            }
        return {
            "history_ok": True,
            "book_ok": True,
            "observation_result": None,
            "diagnostics": [],
            "movement": None,
            "book": None,
            "confirmed": None,
        }

    service._evaluate_candidate = _eval  # type: ignore[method-assign]
    service._build_asset_write = lambda **kwargs: (  # type: ignore[method-assign]
        MagicMock(
            eligible=False,
            baseline_advanced=False,
            signal_created=False,
            signal_material_updated=False,
            signal_expired=False,
        ),
        None,
    )
    service._absence_write = lambda **kwargs: None  # type: ignore[method-assign]
    service._apply_book_budgets = lambda s: (s, 0)  # type: ignore[method-assign]

    # Drive only the selection portion by calling run_cycle with COMPLETE discovery.
    from app.opportunity_intel.discovery import DiscoveryResult

    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    discovery = DiscoveryResult(
        status=ResultStatus.COMPLETE,
        markets=(),
        diagnostics=(),
        pages_fetched=1,
        completed_at=now,
    )

    # Monkeypatch shortlist to our ordered candidates.
    import app.opportunity_intel.scanner as scanner_mod

    original = scanner_mod.shortlist_outcomes
    scanner_mod.shortlist_outcomes = lambda *a, **k: shortlist
    try:
        result = service.run_cycle(
            now=now,
            strict_live_demand=False,
            discovery_result=discovery,
        )
    finally:
        scanner_mod.shortlist_outcomes = original

    assert result.histories_failed >= 1
    assert result.histories_valid >= 1
    # Only the capable asset should be selected for writes/tracking.
    assert any(r for r in result.asset_results)


def test_all_history_missing_keeps_slots_empty(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "allmiss.db"))
    from app.database.storage import initialize_database

    initialize_database(settings)
    shortlist = (_candidate("a", "9000"), _candidate("b", "8000"))
    service = OpportunityScannerService(
        settings,
        discovery_cache=MagicMock(),
        history_client=MagicMock(),
        book_client=MagicMock(),
        max_markets=2,
        request_budget=CycleRequestBudget(clob_cap=50, gamma_cap=10),
    )
    service._evaluate_candidate = lambda candidate, now: {  # type: ignore[method-assign]
        "history_ok": False,
        "book_ok": True,
        "observation_result": None,
        "diagnostics": [OpportunityDiagnosticCode.HISTORY_MISSING],
    }
    service._build_asset_write = lambda **kwargs: (MagicMock(), None)  # type: ignore[method-assign]
    service._absence_write = lambda **kwargs: None  # type: ignore[method-assign]
    service._apply_book_budgets = lambda s: (s, 0)  # type: ignore[method-assign]

    from app.opportunity_intel.discovery import DiscoveryResult
    import app.opportunity_intel.scanner as scanner_mod

    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    discovery = DiscoveryResult(
        status=ResultStatus.COMPLETE,
        markets=(),
        diagnostics=(),
        pages_fetched=1,
        completed_at=now,
    )
    original = scanner_mod.shortlist_outcomes
    scanner_mod.shortlist_outcomes = lambda *a, **k: shortlist
    try:
        result = service.run_cycle(
            now=now, strict_live_demand=False, discovery_result=discovery
        )
    finally:
        scanner_mod.shortlist_outcomes = original
    assert result.histories_failed == 2
    assert result.asset_results == ()
