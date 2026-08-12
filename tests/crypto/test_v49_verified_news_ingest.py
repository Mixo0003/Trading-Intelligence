"""Focused offline tests for V4.9 verified news ingest & source registry."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.crypto.opportunity.news_fundamentals import (
    NewsEventClass,
    NewsFundamentalRejectCode,
    NewsOverlayAction,
    create_signal_from_news_alone,
)
from app.crypto.opportunity.source_registry import (
    FRED_SOURCE_ID,
    AuthoritativeSourceRegistry,
    default_authoritative_registry,
)
from app.crypto.opportunity.unified_scoring import (
    ScoreRejectCode,
    StrategyKind,
    UnifiedScoreRequest,
    ScoreComponent,
    ScoreComponentId,
    StrategyEvidenceRef,
    evaluate_unified_score,
)
from app.crypto.opportunity.verified_news_ingest import (
    IngestRejectCode,
    OfficialFreshnessPolicy,
    apply_verified_news_overlay,
    build_news_evidence_from_observation,
    dedupe_ingested_evidence,
    ingest_eia_inventory_payload,
    ingest_eia_rwtc_payload,
    ingest_fred_dcoilwtico_payload,
    refuse_news_alone_signal,
    resolve_registered_source,
    unsupported_event_class_ingest,
)
from app.crypto.product_types import ProductType
from app.crypto.venue_registry import CanonicalInstrument, V43_EXCHANGE_IDS
from app.market_opportunity.sources.eia import (
    EIA_RWTC_ROUTE,
    EIA_RWTC_SERIES_ID,
    EIA_WTI_INVENTORY_ROUTE,
    EIA_WTI_INVENTORY_SERIES_ID,
    WTI_CUSHING_SOURCE_ID,
)
from app.market_opportunity.sources.fred import (
    FRED_DCOILWTICO_SERIES_ID,
    FRED_OBSERVATIONS_ROUTE,
)
from tests.conftest import make_test_settings


def _now() -> datetime:
    return datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)


def _inst() -> CanonicalInstrument:
    return CanonicalInstrument(
        venue_id="binance",
        product_type=ProductType.SPOT,
        venue_market_id="BTCUSDT",
        base="BTC",
        quote="USDT",
        normalized_symbol="BTC/USDT",
    )


def _rwtc_payload(period: str = "2026-08-11") -> dict:
    return {
        "response": {
            "data": [
                {
                    "period": period,
                    "series": "RWTC",
                    "value": "81.96",
                    "units": "$/BBL",
                }
            ]
        }
    }


def _inventory_payload(period: str = "2026-08-07") -> dict:
    return {
        "response": {
            "data": [
                {
                    "period": period,
                    "series": "WCESTUS1",
                    "value": "406987",
                    "units": "MBBL",
                }
            ]
        }
    }


def _fred_payload(period: str = "2026-08-11") -> dict:
    return {
        "observations": [
            {"date": period, "value": "82.10"},
        ]
    }


def test_source_registry_only_verified_official_sources():
    registry = default_authoritative_registry()
    assert isinstance(registry, AuthoritativeSourceRegistry)
    ids = {(s.source_id, s.series_id) for s in registry.iter_sources()}
    assert (WTI_CUSHING_SOURCE_ID, EIA_RWTC_SERIES_ID) in ids
    assert (WTI_CUSHING_SOURCE_ID, EIA_WTI_INVENTORY_SERIES_ID) in ids
    assert (FRED_SOURCE_ID, FRED_DCOILWTICO_SERIES_ID) in ids
    assert not registry.contains_source_id("bls_cpi")
    entry = registry.require(
        source_id=WTI_CUSHING_SOURCE_ID, series_id=EIA_RWTC_SERIES_ID
    )
    assert entry.endpoint_url == EIA_RWTC_ROUTE
    missing, err = resolve_registered_source(
        registry, source_id="bls_cpi", series_id="CPI"
    )
    assert missing is None
    assert err is not None
    assert err.code is IngestRejectCode.UNSUPPORTED_SOURCE


def test_official_source_validation_and_ingest_normalization():
    obs, err = ingest_eia_rwtc_payload(
        _rwtc_payload(), retrieved_at=_now()
    )
    assert err is None and obs is not None
    assert obs.registry_entry.endpoint_url == EIA_RWTC_ROUTE
    assert obs.period == date(2026, 8, 11)
    assert obs.value == Decimal("81.96")
    assert obs.dedupe_key == "eia:RWTC:2026-08-11"
    assert obs.source_event_at.tzinfo is not None
    assert obs.retrieved_at == _now()

    inv, ierr = ingest_eia_inventory_payload(
        _inventory_payload(), retrieved_at=_now()
    )
    assert ierr is None and inv is not None
    assert inv.registry_entry.endpoint_url == EIA_WTI_INVENTORY_ROUTE

    fred, ferr = ingest_fred_dcoilwtico_payload(
        _fred_payload(), retrieved_at=_now()
    )
    assert ferr is None and fred is not None
    assert fred.registry_entry.endpoint_url == FRED_OBSERVATIONS_ROUTE
    assert fred.dedupe_key.startswith("fred:DCOILWTICO:")


def test_provenance_timestamps_and_deduplication():
    obs, _ = ingest_eia_rwtc_payload(_rwtc_payload(), retrieved_at=_now())
    assert obs is not None
    evidence = build_news_evidence_from_observation(
        obs, related_instrument=_inst()
    )
    assert evidence.provenance.url == EIA_RWTC_ROUTE
    assert evidence.provenance.source_id == WTI_CUSHING_SOURCE_ID
    assert evidence.provenance.published_at == obs.source_event_at
    assert evidence.provenance.retrieved_at == obs.retrieved_at
    assert evidence.related_asset_label == "WTI_CUSHING"
    kept, rejects = dedupe_ingested_evidence((evidence, evidence))
    assert len(kept) == 1
    assert rejects[0].code is NewsFundamentalRejectCode.DUPLICATE_EVIDENCE


def test_freshness_validation_reuses_moi_policy():
    policy = OfficialFreshnessPolicy()
    fresh_obs, _ = ingest_eia_rwtc_payload(
        _rwtc_payload("2026-08-11"), retrieved_at=_now()
    )
    assert fresh_obs is not None
    accepted, err = apply_verified_news_overlay(
        opportunity_uid="opp-1",
        action=NewsOverlayAction.CONFIRM,
        observation=fresh_obs,
        freshness_policy=policy,
        related_instrument=_inst(),
        as_of=_now(),
        now=_now(),
    )
    assert err is None and accepted is not None
    assert accepted.freshness_checked is True
    assert accepted.overlay.action is NewsOverlayAction.CONFIRM

    missing_policy, merr = apply_verified_news_overlay(
        opportunity_uid="opp-1",
        action=NewsOverlayAction.CONFIRM,
        observation=fresh_obs,
        freshness_policy=None,
        related_instrument=_inst(),
        as_of=_now(),
        now=_now(),
    )
    assert missing_policy is None
    assert merr is not None
    assert merr.code is IngestRejectCode.MISSING_FRESHNESS_POLICY

    stale_obs, _ = ingest_eia_rwtc_payload(
        _rwtc_payload("2026-07-01"), retrieved_at=_now()
    )
    assert stale_obs is not None
    stale_acc, serr = apply_verified_news_overlay(
        opportunity_uid="opp-1",
        action=NewsOverlayAction.DOWNGRADE,
        observation=stale_obs,
        freshness_policy=policy,
        related_instrument=_inst(),
        as_of=_now(),
        now=_now(),
    )
    assert stale_acc is None
    assert serr is not None
    assert serr.code is IngestRejectCode.STALE_EVIDENCE


def test_asset_mapping_and_irrelevant_without_instrument():
    obs, _ = ingest_eia_rwtc_payload(_rwtc_payload(), retrieved_at=_now())
    assert obs is not None
    bad, err = apply_verified_news_overlay(
        opportunity_uid="opp-1",
        action=NewsOverlayAction.REJECT,
        observation=obs,
        freshness_policy=OfficialFreshnessPolicy(),
        related_instrument=None,
        as_of=_now(),
        now=_now(),
    )
    assert bad is None
    assert err is not None
    assert err.code is IngestRejectCode.VALIDATION_FAILED


@pytest.mark.parametrize(
    "action",
    [
        NewsOverlayAction.CONFIRM,
        NewsOverlayAction.REJECT,
        NewsOverlayAction.DOWNGRADE,
        NewsOverlayAction.SUPPRESS,
    ],
)
def test_news_overlay_behavior_on_existing_opportunity(action):
    obs, _ = ingest_fred_dcoilwtico_payload(
        _fred_payload(), retrieved_at=_now()
    )
    assert obs is not None
    accepted, err = apply_verified_news_overlay(
        opportunity_uid="crypto_v1:existing",
        action=action,
        observation=obs,
        freshness_policy=OfficialFreshnessPolicy(),
        related_instrument=_inst(),
        as_of=_now(),
        now=_now(),
    )
    assert err is None and accepted is not None
    assert accepted.overlay.opportunity_uid == "crypto_v1:existing"
    assert accepted.overlay.action is action


def test_news_alone_never_creates_signal():
    obs, _ = ingest_eia_rwtc_payload(_rwtc_payload(), retrieved_at=_now())
    assert obs is not None
    evidence = build_news_evidence_from_observation(
        obs, related_instrument=_inst()
    )
    result, reject = refuse_news_alone_signal(evidence)
    assert result is None
    assert reject.code is NewsFundamentalRejectCode.NEWS_ALONE_SIGNAL_FORBIDDEN
    result2, reject2 = create_signal_from_news_alone(evidence)
    assert result2 is None
    assert reject2.code is NewsFundamentalRejectCode.NEWS_ALONE_SIGNAL_FORBIDDEN
    no_uid, err = apply_verified_news_overlay(
        opportunity_uid="",
        action=NewsOverlayAction.CONFIRM,
        observation=obs,
        freshness_policy=OfficialFreshnessPolicy(),
        related_instrument=_inst(),
        as_of=_now(),
        now=_now(),
    )
    assert no_uid is None
    assert err is not None
    assert err.code is IngestRejectCode.MISSING_OPPORTUNITY_REF


def test_unsupported_event_classes_and_invalid_payloads_fail_closed():
    reject = unsupported_event_class_ingest(NewsEventClass.GEOPOLITICS)
    assert reject.code is IngestRejectCode.UNSUPPORTED_EVENT_CLASS
    bad, err = ingest_eia_rwtc_payload({"response": {"data": []}}, retrieved_at=_now())
    assert bad is None
    assert err is not None
    assert err.code is IngestRejectCode.EMPTY_PAYLOAD


def test_unchanged_v44_v48_and_scanner_delivery_safety():
    assert "bybit" not in V43_EXCHANGE_IDS
    settings = make_test_settings()
    assert settings.crypto_opportunity_scanner_enabled is False
    assert settings.crypto_opportunity_delivery_enabled is False

    request = UnifiedScoreRequest(
        opportunity_uid="opp-1",
        strategy_kind=StrategyKind.SPOT_ARBITRAGE,
        components=(
            ScoreComponent(
                component_id=ScoreComponentId.NET_EDGE,
                present=True,
                diagnostic="edge",
            ),
        ),
        evidence_refs=(
            StrategyEvidenceRef(
                strategy_kind=StrategyKind.SPOT_ARBITRAGE,
                opportunity_uid="opp-1",
                evidence_summary="arb",
                as_of=_now(),
            ),
        ),
        as_of=_now(),
    )
    _, score_reject = evaluate_unified_score(request)
    assert score_reject.code is ScoreRejectCode.MISSING_WEIGHT_POLICY

    from app.crypto.opportunity import (
        news_fundamentals,
        source_registry,
        verified_news_ingest,
        scanner,
    )

    blob = (
        inspect.getsource(source_registry)
        + inspect.getsource(verified_news_ingest)
        + inspect.getsource(news_fundamentals)
        + inspect.getsource(scanner)
    ).lower()
    for banned in (
        "api_key",
        "secret_key",
        "create_order",
        "place_order",
        "withdraw",
        "transfer",
        "wallet_balance",
        "private_key",
    ):
        assert banned not in blob
