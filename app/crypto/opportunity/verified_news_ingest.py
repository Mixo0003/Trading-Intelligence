"""V4.9 verified news/economic ingest composed on V4.7 news contracts.

Uses only EIA/FRED adapters and freshness helpers already in the repository.
Does not invent endpoints, TTLs, rankings, weights, confidence, or profit
formulas. Unsupported sources and event classes fail closed.

News may confirm/reject/downgrade/suppress an **existing** opportunity after
registry + freshness + provenance validation. News alone never creates a
signal. Leaves ``news_fundamentals.py`` evaluate ceiling intact for ranking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.crypto.opportunity.news_fundamentals import (
    NewsEvidenceItem,
    NewsEventClass,
    NewsFundamentalReject,
    NewsFundamentalRejectCode,
    NewsOverlayAction,
    NewsProvenance,
    NewsVerificationStatus,
    OpportunityNewsOverlay,
    create_signal_from_news_alone,
    dedupe_news_evidence,
    evaluate_news_fundamental_overlay,
    validate_news_evidence_item,
)
from app.crypto.opportunity.source_registry import (
    AuthoritativeSourceRegistry,
    FRED_SOURCE_ID,
    RegisteredSource,
    RegistrySeriesKind,
    default_authoritative_registry,
)
from app.crypto.venue_registry import CanonicalInstrument
from app.market_opportunity.freshness import FreshnessError, assert_fresh
from app.market_opportunity.models import Provenance
from app.market_opportunity.sources.eia import (
    EIA_RWTC_SERIES_ID,
    EIA_WTI_INVENTORY_SERIES_ID,
    WTI_CUSHING_SOURCE_ID,
    parse_inventory_wcestus1,
    parse_rwtc_daily,
    period_to_source_event_at,
)
from app.market_opportunity.sources.fred import (
    FRED_DCOILWTICO_SERIES_ID,
    parse_dcoilwtico_observations,
)


class IngestRejectCode(StrEnum):
    UNSUPPORTED_SOURCE = "unsupported_source"
    UNSUPPORTED_SERIES = "unsupported_series"
    UNSUPPORTED_EVENT_CLASS = "unsupported_event_class"
    INVALID_PAYLOAD = "invalid_payload"
    EMPTY_PAYLOAD = "empty_payload"
    STALE_EVIDENCE = "stale_evidence"
    MISSING_REGISTRY = "missing_registry"
    MISSING_FRESHNESS_POLICY = "missing_freshness_policy"
    MISSING_OPPORTUNITY_REF = "missing_opportunity_ref"
    VALIDATION_FAILED = "validation_failed"
    NEWS_ALONE_FORBIDDEN = "news_alone_forbidden"


@dataclass(frozen=True)
class IngestReject:
    code: IngestRejectCode
    message: str
    source_id: str | None = None
    series_id: str | None = None


@dataclass(frozen=True)
class OfficialFreshnessPolicy:
    """Injectable freshness policy wrapping MOI ``assert_fresh`` only.

    Does not invent new TTL constants — reuses repository freshness helpers.
    """

    def assert_official_fresh(
        self,
        *,
        source_id: str,
        series_kind: str,
        source_event_at: datetime,
        retrieved_at: datetime,
        now: datetime | None = None,
    ) -> None:
        provenance = Provenance(
            source_id=source_id,
            fetched_at=retrieved_at,
            source_event_at=source_event_at,
            series_kind=series_kind,
        )
        assert_fresh(provenance, now=now)


@dataclass(frozen=True)
class IngestedOfficialObservation:
    """Normalized official observation before news-evidence materialization."""

    registry_entry: RegisteredSource
    period: date
    value: Decimal
    source_event_at: datetime
    retrieved_at: datetime
    dedupe_key: str


@dataclass(frozen=True)
class VerifiedOverlayAcceptance:
    """Validated news overlay on an existing opportunity (not a trade signal)."""

    overlay: OpportunityNewsOverlay
    registry_entry: RegisteredSource
    freshness_checked: bool


def _reject(
    code: IngestRejectCode,
    message: str,
    *,
    source_id: str | None = None,
    series_id: str | None = None,
) -> tuple[None, IngestReject]:
    return None, IngestReject(
        code=code, message=message, source_id=source_id, series_id=series_id
    )


def resolve_registered_source(
    registry: AuthoritativeSourceRegistry,
    *,
    source_id: str,
    series_id: str,
) -> tuple[RegisteredSource | None, IngestReject | None]:
    entry = registry.get(source_id=source_id, series_id=series_id)
    if entry is None:
        return None, IngestReject(
            code=IngestRejectCode.UNSUPPORTED_SOURCE,
            message=(
                f"source/series not in authoritative registry "
                f"({source_id!r}/{series_id!r}); fail closed"
            ),
            source_id=source_id,
            series_id=series_id,
        )
    return entry, None


def ingest_eia_rwtc_payload(
    payload: dict[str, Any],
    *,
    registry: AuthoritativeSourceRegistry | None = None,
    retrieved_at: datetime | None = None,
) -> tuple[IngestedOfficialObservation | None, IngestReject | None]:
    """Offline-capable ingest using existing ``parse_rwtc_daily``."""
    reg = registry or default_authoritative_registry()
    entry, err = resolve_registered_source(
        reg, source_id=WTI_CUSHING_SOURCE_ID, series_id=EIA_RWTC_SERIES_ID
    )
    if err is not None or entry is None:
        return None, err
    try:
        bars = parse_rwtc_daily(payload)
    except Exception as exc:  # noqa: BLE001 — adapter raises EIAError/ValueError
        return _reject(
            IngestRejectCode.INVALID_PAYLOAD,
            f"EIA RWTC parse failed: {exc}",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    if not bars:
        return _reject(
            IngestRejectCode.EMPTY_PAYLOAD,
            "EIA RWTC payload produced no bars",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    latest = max(bars, key=lambda bar: bar.period)
    clock = retrieved_at or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        return _reject(
            IngestRejectCode.INVALID_PAYLOAD,
            "retrieved_at must be timezone-aware",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    event_at = period_to_source_event_at(latest.period)
    return (
        IngestedOfficialObservation(
            registry_entry=entry,
            period=latest.period,
            value=latest.close,
            source_event_at=event_at,
            retrieved_at=clock.astimezone(timezone.utc),
            dedupe_key=f"eia:{entry.series_id}:{latest.period.isoformat()}",
        ),
        None,
    )


def ingest_eia_inventory_payload(
    payload: dict[str, Any],
    *,
    registry: AuthoritativeSourceRegistry | None = None,
    retrieved_at: datetime | None = None,
) -> tuple[IngestedOfficialObservation | None, IngestReject | None]:
    reg = registry or default_authoritative_registry()
    entry, err = resolve_registered_source(
        reg,
        source_id=WTI_CUSHING_SOURCE_ID,
        series_id=EIA_WTI_INVENTORY_SERIES_ID,
    )
    if err is not None or entry is None:
        return None, err
    try:
        points = parse_inventory_wcestus1(payload)
    except Exception as exc:  # noqa: BLE001
        return _reject(
            IngestRejectCode.INVALID_PAYLOAD,
            f"EIA inventory parse failed: {exc}",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    if not points:
        return _reject(
            IngestRejectCode.EMPTY_PAYLOAD,
            "EIA inventory payload produced no points",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    latest = max(points, key=lambda point: point.period)
    clock = retrieved_at or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        return _reject(
            IngestRejectCode.INVALID_PAYLOAD,
            "retrieved_at must be timezone-aware",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    event_at = period_to_source_event_at(latest.period)
    return (
        IngestedOfficialObservation(
            registry_entry=entry,
            period=latest.period,
            value=latest.value,
            source_event_at=event_at,
            retrieved_at=clock.astimezone(timezone.utc),
            dedupe_key=f"eia:{entry.series_id}:{latest.period.isoformat()}",
        ),
        None,
    )


def ingest_fred_dcoilwtico_payload(
    payload: dict[str, Any],
    *,
    registry: AuthoritativeSourceRegistry | None = None,
    retrieved_at: datetime | None = None,
) -> tuple[IngestedOfficialObservation | None, IngestReject | None]:
    reg = registry or default_authoritative_registry()
    entry, err = resolve_registered_source(
        reg, source_id=FRED_SOURCE_ID, series_id=FRED_DCOILWTICO_SERIES_ID
    )
    if err is not None or entry is None:
        return None, err
    try:
        observations = parse_dcoilwtico_observations(payload)
    except Exception as exc:  # noqa: BLE001
        return _reject(
            IngestRejectCode.INVALID_PAYLOAD,
            f"FRED DCOILWTICO parse failed: {exc}",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    if not observations:
        return _reject(
            IngestRejectCode.EMPTY_PAYLOAD,
            "FRED payload produced no observations",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    latest = max(observations, key=lambda item: item.period)
    clock = retrieved_at or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        return _reject(
            IngestRejectCode.INVALID_PAYLOAD,
            "retrieved_at must be timezone-aware",
            source_id=entry.source_id,
            series_id=entry.series_id,
        )
    event_at = period_to_source_event_at(latest.period)
    return (
        IngestedOfficialObservation(
            registry_entry=entry,
            period=latest.period,
            value=latest.value,
            source_event_at=event_at,
            retrieved_at=clock.astimezone(timezone.utc),
            dedupe_key=f"fred:{entry.series_id}:{latest.period.isoformat()}",
        ),
        None,
    )


def event_class_for_registry_entry(entry: RegisteredSource) -> NewsEventClass:
    if entry.series_kind in {
        RegistrySeriesKind.EIA_DAILY_SPOT,
        RegistrySeriesKind.EIA_WEEKLY_INVENTORY,
        RegistrySeriesKind.FRED_DAILY_CROSSCHECK,
    }:
        return NewsEventClass.EIA_OPEC
    raise ValueError(f"unsupported registry series_kind {entry.series_kind}")


def build_news_evidence_from_observation(
    observation: IngestedOfficialObservation,
    *,
    related_instrument: CanonicalInstrument | None = None,
    summary: str | None = None,
) -> NewsEvidenceItem:
    entry = observation.registry_entry
    text = summary or (
        f"{entry.source_id} {entry.series_id} {observation.period.isoformat()} "
        f"value={observation.value}"
    )
    return NewsEvidenceItem(
        evidence_id=observation.dedupe_key,
        event_class=event_class_for_registry_entry(entry),
        related_asset_label=entry.asset_label,
        related_instrument=related_instrument,
        dedupe_key=observation.dedupe_key,
        verification_status=NewsVerificationStatus.VERIFIED,
        provenance=NewsProvenance(
            source_id=entry.source_id,
            publisher=entry.publisher,
            source_type=entry.source_type,
            url=entry.endpoint_url,
            published_at=observation.source_event_at,
            retrieved_at=observation.retrieved_at,
            note=entry.note,
        ),
        summary=text,
        asset_rules=None,
    )


def apply_freshness_policy(
    observation: IngestedOfficialObservation,
    policy: OfficialFreshnessPolicy | None,
    *,
    now: datetime | None = None,
) -> IngestReject | None:
    if policy is None:
        return IngestReject(
            code=IngestRejectCode.MISSING_FRESHNESS_POLICY,
            message=(
                "freshness policy required; V4.9 does not invent TTLs "
                "(pass OfficialFreshnessPolicy wrapping MOI assert_fresh)"
            ),
            source_id=observation.registry_entry.source_id,
            series_id=observation.registry_entry.series_id,
        )
    try:
        policy.assert_official_fresh(
            source_id=observation.registry_entry.source_id,
            series_kind=observation.registry_entry.series_kind.value,
            source_event_at=observation.source_event_at,
            retrieved_at=observation.retrieved_at,
            now=now,
        )
    except FreshnessError as exc:
        return IngestReject(
            code=IngestRejectCode.STALE_EVIDENCE,
            message=str(exc),
            source_id=observation.registry_entry.source_id,
            series_id=observation.registry_entry.series_id,
        )
    return None


def apply_verified_news_overlay(
    *,
    opportunity_uid: str,
    action: NewsOverlayAction,
    observation: IngestedOfficialObservation,
    registry: AuthoritativeSourceRegistry | None = None,
    freshness_policy: OfficialFreshnessPolicy | None = None,
    related_instrument: CanonicalInstrument | None = None,
    as_of: datetime | None = None,
    now: datetime | None = None,
) -> tuple[VerifiedOverlayAcceptance | None, IngestReject | None]:
    """Attach verified official evidence to an existing opportunity only."""
    if not opportunity_uid:
        return None, IngestReject(
            code=IngestRejectCode.MISSING_OPPORTUNITY_REF,
            message="existing opportunity_uid is required",
        )
    reg = registry or default_authoritative_registry()
    resolved, err = resolve_registered_source(
        reg,
        source_id=observation.registry_entry.source_id,
        series_id=observation.registry_entry.series_id,
    )
    if err is not None or resolved is None:
        return None, err

    freshness_err = apply_freshness_policy(
        observation, freshness_policy, now=now
    )
    if freshness_err is not None:
        return None, freshness_err

    evidence = build_news_evidence_from_observation(
        observation, related_instrument=related_instrument
    )
    # Without a crypto instrument, EIA/FRED overlays still need asset mapping
    # via related_asset_label WTI_CUSHING; validate_news_evidence_item requires
    # instrument or asset_rules for non-trading-rules events.
    if evidence.related_instrument is None:
        validation = validate_news_evidence_item(evidence)
        if validation is not None:
            # Allow official oil series as evidence attached to a crypto opp
            # only when caller supplies related_instrument for asset mapping.
            return None, IngestReject(
                code=IngestRejectCode.VALIDATION_FAILED,
                message=(
                    f"{validation.message}; provide related_instrument for "
                    "event-to-asset mapping onto an existing opportunity"
                ),
                source_id=resolved.source_id,
                series_id=resolved.series_id,
            )

    validation = validate_news_evidence_item(evidence)
    if validation is not None:
        return None, IngestReject(
            code=IngestRejectCode.VALIDATION_FAILED,
            message=validation.message,
            source_id=resolved.source_id,
            series_id=resolved.series_id,
        )

    alone, alone_reject = create_signal_from_news_alone(evidence)
    assert alone is None
    assert (
        alone_reject.code is NewsFundamentalRejectCode.NEWS_ALONE_SIGNAL_FORBIDDEN
    )

    clock = as_of or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        return None, IngestReject(
            code=IngestRejectCode.INVALID_PAYLOAD,
            message="as_of must be timezone-aware",
        )
    overlay = OpportunityNewsOverlay(
        opportunity_uid=opportunity_uid,
        action=action,
        evidence=evidence,
        as_of=clock.astimezone(timezone.utc),
    )

    # V4.7 evaluate still fail-closes without ranking (UNSUPPORTED_OVERLAY).
    # V4.9 acceptance means registry+freshness+validation passed for an
    # existing opportunity overlay — not a scored trade signal.
    _, fund_reject = evaluate_news_fundamental_overlay(
        overlay,
        source_registry=reg,
        freshness_policy=freshness_policy,
    )
    if fund_reject.code not in {
        NewsFundamentalRejectCode.UNSUPPORTED_OVERLAY,
    }:
        return None, IngestReject(
            code=IngestRejectCode.VALIDATION_FAILED,
            message=fund_reject.message,
            source_id=resolved.source_id,
            series_id=resolved.series_id,
        )

    return (
        VerifiedOverlayAcceptance(
            overlay=overlay,
            registry_entry=resolved,
            freshness_checked=True,
        ),
        None,
    )


def refuse_news_alone_signal(
    evidence: NewsEvidenceItem,
) -> tuple[None, NewsFundamentalReject]:
    """Explicit V4.9 surface for the hard news-alone rule."""
    return create_signal_from_news_alone(evidence)


def dedupe_ingested_evidence(
    items: tuple[NewsEvidenceItem, ...],
) -> tuple[tuple[NewsEvidenceItem, ...], tuple[NewsFundamentalReject, ...]]:
    return dedupe_news_evidence(items)


def unsupported_event_class_ingest(
    event_class: NewsEventClass,
) -> IngestReject:
    """Event classes without verified adapters fail closed."""
    supported = {NewsEventClass.EIA_OPEC, NewsEventClass.OFFICIAL_ECONOMIC_RELEASE}
    if event_class in supported:
        return IngestReject(
            code=IngestRejectCode.UNSUPPORTED_EVENT_CLASS,
            message=(
                f"{event_class.value} label is recognized but requires a "
                "registered EIA/FRED series adapter invocation"
            ),
        )
    return IngestReject(
        code=IngestRejectCode.UNSUPPORTED_EVENT_CLASS,
        message=(
            f"{event_class.value} has no verified ingest adapter in repository; "
            "fail closed (do not invent endpoints)"
        ),
    )
