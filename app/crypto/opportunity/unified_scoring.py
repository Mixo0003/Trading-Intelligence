"""V4.8 unified scoring and calibration foundation (contracts + validation).

One universal score/calibration contract for Market Intelligence strategies:
spot cross-exchange arbitrage, basis/funding, directional intelligence, and
news/fundamental overlays.

Normalizes strategy evidence **references** and provenance labels. Keeps
strategy-specific payloads separate from the universal score contract.

Does **not** invent numeric weights, thresholds, confidence/probability
formulas, ROI formulas, or ranking policies. Evaluation fails closed until
approved policy objects exist. V4.10 adds explicit ``scoring_policy`` contracts
and deterministic ranking **only** when a complete approved ``ScoringPolicy``
is supplied (no baked-in defaults). Does not rewrite historical outcomes or
fabricate measurements. Does not change V4.4–V4.9 modules, scanner, or
delivery defaults.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from app.crypto.opportunity.basis_funding import (
    BasisObservation,
    FundingObservation,
)
from app.crypto.opportunity.directional import DirectionalObservation
from app.crypto.opportunity.models import PaperNetOpportunity
from app.crypto.opportunity.news_fundamentals import OpportunityNewsOverlay


class StrategyKind(StrEnum):
    """Strategy family for unified scoring — labels only."""

    SPOT_ARBITRAGE = "spot_arbitrage"
    BASIS = "basis"
    FUNDING = "funding"
    DIRECTIONAL = "directional"
    NEWS_FUNDAMENTAL = "news_fundamental"


class ScoreComponentId(StrEnum):
    """Explainable component identifiers — not weighted scores."""

    NET_EDGE = "net_edge"
    LIQUIDITY = "liquidity"
    FRESHNESS = "freshness"
    FEE_DRAG = "fee_drag"
    EVIDENCE_COMPLETENESS = "evidence_completeness"
    NEWS_OVERLAY = "news_overlay"
    PRODUCT_SUPPORT = "product_support"
    PROVENANCE = "provenance"


class ScoreRejectCode(StrEnum):
    MISSING_WEIGHT_POLICY = "missing_weight_policy"
    MISSING_THRESHOLD_POLICY = "missing_threshold_policy"
    MISSING_RANKING_POLICY = "missing_ranking_policy"
    MISSING_REQUIRED_INPUT = "missing_required_input"
    STALE_EVIDENCE = "stale_evidence"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    UNSUPPORTED_STRATEGY = "unsupported_strategy"
    INVALID_SCORE_REQUEST = "invalid_score_request"
    SCORE_UNSUPPORTED = "score_unsupported"


class OutcomeStatus(StrEnum):
    """Paper outcome lifecycle for calibration — not a probability."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    UNOBSERVABLE = "unobservable"


_TERMINAL_OUTCOMES = frozenset(OutcomeStatus)


@dataclass(frozen=True)
class ScoreReject:
    code: ScoreRejectCode
    message: str
    opportunity_uid: str | None = None
    strategy_kind: str | None = None


def _identifier(value: str, field_name: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must have a UTC offset")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ScoreComponent:
    """One explainable score component — diagnostic label only."""

    component_id: ScoreComponentId
    present: bool
    diagnostic: str
    provenance_note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.component_id, ScoreComponentId):
            raise TypeError("component_id must be ScoreComponentId")
        if not isinstance(self.present, bool):
            raise TypeError("present must be bool")
        _identifier(self.diagnostic, "diagnostic", maximum=500)
        if self.provenance_note:
            _identifier(self.provenance_note, "provenance_note", maximum=500)


@dataclass(frozen=True)
class StrategyEvidenceRef:
    """Normalized reference to strategy-specific evidence (not a score)."""

    strategy_kind: StrategyKind
    opportunity_uid: str
    evidence_summary: str
    as_of: datetime
    provenance_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.strategy_kind, StrategyKind):
            raise TypeError("strategy_kind must be StrategyKind")
        _identifier(self.opportunity_uid, "opportunity_uid", maximum=256)
        _identifier(self.evidence_summary, "evidence_summary", maximum=1000)
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        cleaned: list[str] = []
        for item in self.provenance_ids:
            cleaned.append(_identifier(item, "provenance_ids item", maximum=256))
        object.__setattr__(self, "provenance_ids", tuple(cleaned))


@dataclass(frozen=True)
class UnifiedScoreRequest:
    """Request to score an existing opportunity with normalized evidence refs."""

    opportunity_uid: str
    strategy_kind: StrategyKind
    components: tuple[ScoreComponent, ...]
    evidence_refs: tuple[StrategyEvidenceRef, ...]
    as_of: datetime
    notes: str = ""

    def __post_init__(self) -> None:
        _identifier(self.opportunity_uid, "opportunity_uid", maximum=256)
        if not isinstance(self.strategy_kind, StrategyKind):
            raise TypeError("strategy_kind must be StrategyKind")
        if not self.components:
            raise ValueError("components must not be empty")
        seen: set[ScoreComponentId] = set()
        for component in self.components:
            if not isinstance(component, ScoreComponent):
                raise TypeError("components must contain ScoreComponent")
            if component.component_id in seen:
                raise ValueError(
                    f"duplicate score component {component.component_id.value}"
                )
            seen.add(component.component_id)
        for ref in self.evidence_refs:
            if not isinstance(ref, StrategyEvidenceRef):
                raise TypeError("evidence_refs must contain StrategyEvidenceRef")
            if ref.opportunity_uid != self.opportunity_uid:
                raise ValueError(
                    "evidence_refs opportunity_uid must match score request"
                )
            if ref.strategy_kind != self.strategy_kind:
                raise ValueError(
                    "evidence_refs strategy_kind must match score request"
                )
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        if self.notes:
            _identifier(self.notes, "notes", maximum=500)


@dataclass(frozen=True)
class OpportunityOutcomeRecord:
    """Immutable paper outcome for calibration once recorded."""

    opportunity_uid: str
    status: OutcomeStatus
    recorded_at: datetime
    observed_at: datetime | None = None
    note: str = ""
    immutable: bool = True

    def __post_init__(self) -> None:
        _identifier(self.opportunity_uid, "opportunity_uid", maximum=256)
        if not isinstance(self.status, OutcomeStatus):
            raise TypeError("status must be OutcomeStatus")
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))
        if self.observed_at is not None:
            object.__setattr__(
                self, "observed_at", _utc(self.observed_at, "observed_at")
            )
            if self.observed_at > self.recorded_at:
                raise ValueError("observed_at must not be after recorded_at")
        if self.note:
            _identifier(self.note, "note", maximum=500)
        if not isinstance(self.immutable, bool):
            raise TypeError("immutable must be bool")
        if self.status in _TERMINAL_OUTCOMES and not self.immutable:
            raise ValueError("terminal outcomes must be immutable")


@dataclass(frozen=True)
class CalibrationRecord:
    """Calibration linkage — does not invent probabilities or rewrite history."""

    calibration_id: str
    opportunity_uid: str
    outcome: OpportunityOutcomeRecord
    as_of: datetime
    policy_version_note: str = "unspecified"

    def __post_init__(self) -> None:
        _identifier(self.calibration_id, "calibration_id", maximum=256)
        _identifier(self.opportunity_uid, "opportunity_uid", maximum=256)
        if not isinstance(self.outcome, OpportunityOutcomeRecord):
            raise TypeError("outcome must be OpportunityOutcomeRecord")
        if self.outcome.opportunity_uid != self.opportunity_uid:
            raise ValueError("calibration opportunity_uid must match outcome")
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        _identifier(self.policy_version_note, "policy_version_note", maximum=128)


def normalize_spot_arbitrage_evidence(
    opportunity: PaperNetOpportunity,
) -> StrategyEvidenceRef:
    if not isinstance(opportunity, PaperNetOpportunity):
        raise TypeError("opportunity must be PaperNetOpportunity")
    return StrategyEvidenceRef(
        strategy_kind=StrategyKind.SPOT_ARBITRAGE,
        opportunity_uid=opportunity.opportunity_uid,
        evidence_summary=(
            f"spot arb {opportunity.pair} {opportunity.buy_venue}->{opportunity.sell_venue}"
        ),
        as_of=opportunity.checked_at
        if opportunity.checked_at.tzinfo
        else opportunity.checked_at.replace(tzinfo=timezone.utc),
        provenance_ids=(
            f"buy:{opportunity.buy_venue}",
            f"sell:{opportunity.sell_venue}",
            "evidence:public_book_and_fees",
        ),
    )


def normalize_basis_evidence(observation: BasisObservation) -> StrategyEvidenceRef:
    if not isinstance(observation, BasisObservation):
        raise TypeError("observation must be BasisObservation")
    uid = (
        f"basis:{observation.relationship}:"
        f"{observation.left.instrument.normalized_symbol}"
    )
    return StrategyEvidenceRef(
        strategy_kind=StrategyKind.BASIS,
        opportunity_uid=uid,
        evidence_summary=f"basis {observation.relationship}",
        as_of=observation.as_of,
        provenance_ids=(
            observation.left.provenance.source_id,
            observation.right.provenance.source_id,
        ),
    )


def normalize_funding_evidence(
    observation: FundingObservation,
) -> StrategyEvidenceRef:
    if not isinstance(observation, FundingObservation):
        raise TypeError("observation must be FundingObservation")
    uid = f"funding:{observation.instrument.identity_key[0]}:{observation.instrument.identity_key[2]}"
    return StrategyEvidenceRef(
        strategy_kind=StrategyKind.FUNDING,
        opportunity_uid=uid,
        evidence_summary="funding observation",
        as_of=observation.as_of,
        provenance_ids=(observation.snapshot.provenance.source_id,),
    )


def normalize_directional_evidence(
    observation: DirectionalObservation,
) -> StrategyEvidenceRef:
    if not isinstance(observation, DirectionalObservation):
        raise TypeError("observation must be DirectionalObservation")
    uid = (
        f"directional:{observation.side.value}:"
        f"{observation.strategy_type.value}:"
        f"{observation.instrument.normalized_symbol}:"
        f"{observation.instrument.venue_id}"
    )
    return StrategyEvidenceRef(
        strategy_kind=StrategyKind.DIRECTIONAL,
        opportunity_uid=uid,
        evidence_summary=(
            f"{observation.side.value} {observation.strategy_type.value}"
        ),
        as_of=observation.as_of,
        provenance_ids=(f"venue:{observation.instrument.venue_id}",),
    )


def normalize_news_evidence(overlay: OpportunityNewsOverlay) -> StrategyEvidenceRef:
    if not isinstance(overlay, OpportunityNewsOverlay):
        raise TypeError("overlay must be OpportunityNewsOverlay")
    return StrategyEvidenceRef(
        strategy_kind=StrategyKind.NEWS_FUNDAMENTAL,
        opportunity_uid=overlay.opportunity_uid,
        evidence_summary=(
            f"news {overlay.action.value} {overlay.evidence.event_class.value}"
        ),
        as_of=overlay.as_of,
        provenance_ids=(
            overlay.evidence.provenance.source_id,
            overlay.evidence.dedupe_key,
        ),
    )


def reject_stale_or_contradictory_components(
    request: UnifiedScoreRequest,
) -> ScoreReject | None:
    """Fail closed on contradictory present/absent diagnostics."""
    by_id = {c.component_id: c for c in request.components}
    freshness = by_id.get(ScoreComponentId.FRESHNESS)
    if freshness is not None and freshness.present is False:
        if "stale" in freshness.diagnostic.lower():
            return ScoreReject(
                code=ScoreRejectCode.STALE_EVIDENCE,
                message=freshness.diagnostic,
                opportunity_uid=request.opportunity_uid,
                strategy_kind=request.strategy_kind.value,
            )
    provenance = by_id.get(ScoreComponentId.PROVENANCE)
    evidence = by_id.get(ScoreComponentId.EVIDENCE_COMPLETENESS)
    if (
        provenance is not None
        and evidence is not None
        and provenance.present
        and not evidence.present
        and "contradict" in evidence.diagnostic.lower()
    ):
        return ScoreReject(
            code=ScoreRejectCode.CONTRADICTORY_EVIDENCE,
            message=evidence.diagnostic,
            opportunity_uid=request.opportunity_uid,
            strategy_kind=request.strategy_kind.value,
        )
    return None


def evaluate_unified_score(
    request: UnifiedScoreRequest,
    *,
    weight_policy: object | None = None,
    threshold_policy: object | None = None,
    ranking_policy: object | None = None,
) -> tuple[None, ScoreReject]:
    """Foundation evaluator — always fail closed without approved policies.

    Never returns a numeric score, probability, or guaranteed-profit claim.
    """
    if not isinstance(request, UnifiedScoreRequest):
        raise TypeError("request must be UnifiedScoreRequest")
    if not request.evidence_refs:
        return None, ScoreReject(
            code=ScoreRejectCode.MISSING_REQUIRED_INPUT,
            message="unified score requires at least one strategy evidence ref",
            opportunity_uid=request.opportunity_uid,
            strategy_kind=request.strategy_kind.value,
        )
    contradictory = reject_stale_or_contradictory_components(request)
    if contradictory is not None:
        return None, contradictory
    if weight_policy is None:
        return None, ScoreReject(
            code=ScoreRejectCode.MISSING_WEIGHT_POLICY,
            message=(
                "unified score unsupported: no approved weight policy "
                "(V4.8 foundation does not invent numeric weights)"
            ),
            opportunity_uid=request.opportunity_uid,
            strategy_kind=request.strategy_kind.value,
        )
    if threshold_policy is None:
        return None, ScoreReject(
            code=ScoreRejectCode.MISSING_THRESHOLD_POLICY,
            message=(
                "unified score unsupported: no approved threshold policy "
                "(V4.8 foundation does not invent thresholds)"
            ),
            opportunity_uid=request.opportunity_uid,
            strategy_kind=request.strategy_kind.value,
        )
    if ranking_policy is None:
        return None, ScoreReject(
            code=ScoreRejectCode.MISSING_RANKING_POLICY,
            message=(
                "unified score unsupported: no approved ranking policy "
                "(V4.8 foundation does not invent ranking)"
            ),
            opportunity_uid=request.opportunity_uid,
            strategy_kind=request.strategy_kind.value,
        )
    return None, ScoreReject(
        code=ScoreRejectCode.SCORE_UNSUPPORTED,
        message=(
            "unified score unsupported: complete scoring/calibration engine "
            "is not implemented in V4.8 foundation"
        ),
        opportunity_uid=request.opportunity_uid,
        strategy_kind=request.strategy_kind.value,
    )


def record_opportunity_outcome(
    *,
    opportunity_uid: str,
    status: OutcomeStatus,
    recorded_at: datetime,
    observed_at: datetime | None = None,
    note: str = "",
    existing: OpportunityOutcomeRecord | None = None,
) -> tuple[OpportunityOutcomeRecord | None, ScoreReject | None]:
    """Record a terminal paper outcome; refuse to rewrite an existing record."""
    if existing is not None:
        if existing.immutable:
            return None, ScoreReject(
                code=ScoreRejectCode.INVALID_SCORE_REQUEST,
                message=(
                    "outcome already recorded and immutable; "
                    "historical records must not be rewritten"
                ),
                opportunity_uid=opportunity_uid,
            )
    try:
        record = OpportunityOutcomeRecord(
            opportunity_uid=opportunity_uid,
            status=status,
            recorded_at=recorded_at,
            observed_at=observed_at,
            note=note,
            immutable=True,
        )
    except (TypeError, ValueError) as exc:
        return None, ScoreReject(
            code=ScoreRejectCode.INVALID_SCORE_REQUEST,
            message=str(exc),
            opportunity_uid=opportunity_uid,
        )
    return record, None


def build_calibration_record(
    *,
    calibration_id: str,
    outcome: OpportunityOutcomeRecord,
    as_of: datetime,
    policy_version_note: str = "unspecified",
) -> tuple[CalibrationRecord | None, ScoreReject | None]:
    """Build a calibration linkage from a real recorded outcome only."""
    if not isinstance(outcome, OpportunityOutcomeRecord):
        return None, ScoreReject(
            code=ScoreRejectCode.MISSING_REQUIRED_INPUT,
            message="calibration requires a recorded OpportunityOutcomeRecord",
        )
    try:
        record = CalibrationRecord(
            calibration_id=calibration_id,
            opportunity_uid=outcome.opportunity_uid,
            outcome=outcome,
            as_of=as_of,
            policy_version_note=policy_version_note,
        )
    except (TypeError, ValueError) as exc:
        return None, ScoreReject(
            code=ScoreRejectCode.INVALID_SCORE_REQUEST,
            message=str(exc),
            opportunity_uid=outcome.opportunity_uid,
        )
    return record, None


def format_unified_score_status_card(
    request: UnifiedScoreRequest,
    reject: ScoreReject,
) -> str:
    """Honest paper/read-only status card — not a probability or guarantee."""
    lines = [
        "UNIFIED SCORE / CALIBRATION (foundation)",
        f"Opportunity: {request.opportunity_uid}",
        f"Strategy: {request.strategy_kind.value}",
        f"As of: {request.as_of.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "Explainable components:",
    ]
    for component in request.components:
        flag = "present" if component.present else "missing"
        lines.append(
            f"- {component.component_id.value}: {flag} — {component.diagnostic}"
        )
    if request.evidence_refs:
        lines.append("")
        lines.append("Evidence refs:")
        for ref in request.evidence_refs:
            lines.append(f"- {ref.evidence_summary}")
    lines.extend(
        [
            "",
            f"Status: unsupported ({reject.code.value})",
            reject.message,
            "",
            "Public read-only / paper-only. Not guaranteed profit.",
            "Score is not a probability or win-rate.",
            "No invented weights, thresholds, ranking, or ROI formulas.",
        ]
    )
    return "\n".join(lines)
