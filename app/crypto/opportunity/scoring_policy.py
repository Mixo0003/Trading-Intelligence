"""V4.10 scoring policy contracts and ranking enablement.

Extends the V4.8 unified scoring foundation with explicit, validated policy
contracts for weights, thresholds, ranking keys, confidence labels, and score
versions.

Deterministic ranking runs **only** when a complete approved
``ScoringPolicy`` is supplied. There is no baked-in default policy and no
invented numeric weights, probabilities, ROI, or guaranteed-profit claims.

Does not rewrite historical outcomes. Does not change V4.4–V4.9 modules,
scanner defaults, or delivery defaults.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from app.crypto.opportunity.unified_scoring import (
    ScoreComponent,
    ScoreComponentId,
    ScoreReject,
    ScoreRejectCode,
    UnifiedScoreRequest,
    reject_stale_or_contradictory_components,
)


class RankingSortKey(StrEnum):
    """Deterministic sort keys — order is policy-defined, not invented."""

    SCORE_DESC = "score_desc"
    SCORE_ASC = "score_asc"
    AS_OF_DESC = "as_of_desc"
    AS_OF_ASC = "as_of_asc"
    OPPORTUNITY_UID_ASC = "opportunity_uid_asc"
    OPPORTUNITY_UID_DESC = "opportunity_uid_desc"


class PolicyRejectCode(StrEnum):
    MISSING_POLICY = "missing_policy"
    POLICY_NOT_APPROVED = "policy_not_approved"
    INCOMPLETE_POLICY = "incomplete_policy"
    CONTRADICTORY_POLICY = "contradictory_policy"
    MISSING_WEIGHT_POLICY = "missing_weight_policy"
    MISSING_THRESHOLD_POLICY = "missing_threshold_policy"
    MISSING_RANKING_POLICY = "missing_ranking_policy"
    MISSING_CONFIDENCE_POLICY = "missing_confidence_policy"
    INVALID_SCORE_VERSION = "invalid_score_version"
    MIXED_SCORE_VERSION = "mixed_score_version"
    BELOW_THRESHOLD = "below_threshold"
    RANKING_UNSUPPORTED = "ranking_unsupported"


@dataclass(frozen=True)
class PolicyReject:
    code: PolicyRejectCode
    message: str
    score_version: str | None = None


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


def _decimal(value: Decimal, field_name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal")
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"{field_name} must be finite")
    return value


@dataclass(frozen=True)
class WeightPolicy:
    """Explicit component weights for one score version — no defaults."""

    score_version: str
    provenance_note: str
    approved: bool
    component_weights: tuple[tuple[ScoreComponentId, Decimal], ...]
    required_components: tuple[ScoreComponentId, ...]

    def __post_init__(self) -> None:
        _identifier(self.score_version, "score_version", maximum=128)
        _identifier(self.provenance_note, "provenance_note", maximum=500)
        if not isinstance(self.approved, bool):
            raise TypeError("approved must be bool")
        if not self.component_weights:
            raise ValueError("component_weights must not be empty")
        seen: set[ScoreComponentId] = set()
        cleaned: list[tuple[ScoreComponentId, Decimal]] = []
        total = Decimal("0")
        for component_id, weight in self.component_weights:
            if not isinstance(component_id, ScoreComponentId):
                raise TypeError("component_weights keys must be ScoreComponentId")
            if component_id in seen:
                raise ValueError(f"duplicate weight for {component_id.value}")
            seen.add(component_id)
            w = _decimal(weight, "component weight")
            if w < Decimal("0"):
                raise ValueError("component weights must be non-negative")
            total += w
            cleaned.append((component_id, w))
        if total != Decimal("1"):
            raise ValueError("component weights must sum to exactly 1")
        object.__setattr__(self, "component_weights", tuple(cleaned))
        req: list[ScoreComponentId] = []
        for component_id in self.required_components:
            if not isinstance(component_id, ScoreComponentId):
                raise TypeError("required_components must be ScoreComponentId")
            if component_id not in seen:
                raise ValueError(
                    "required_components must be covered by component_weights"
                )
            if component_id in req:
                raise ValueError(f"duplicate required component {component_id.value}")
            req.append(component_id)
        if not req:
            raise ValueError("required_components must not be empty")
        object.__setattr__(self, "required_components", tuple(req))

    def weight_map(self) -> dict[ScoreComponentId, Decimal]:
        return dict(self.component_weights)


@dataclass(frozen=True)
class ThresholdPolicy:
    """Inclusion threshold for scored opportunities — explicit only."""

    score_version: str
    provenance_note: str
    approved: bool
    inclusion_min: Decimal

    def __post_init__(self) -> None:
        _identifier(self.score_version, "score_version", maximum=128)
        _identifier(self.provenance_note, "provenance_note", maximum=500)
        if not isinstance(self.approved, bool):
            raise TypeError("approved must be bool")
        lo = _decimal(self.inclusion_min, "inclusion_min")
        if lo < Decimal("0") or lo > Decimal("1"):
            raise ValueError("inclusion_min must be in [0, 1]")
        object.__setattr__(self, "inclusion_min", lo)


@dataclass(frozen=True)
class RankingPolicy:
    """Deterministic ranking key order — must include a UID tie-breaker."""

    score_version: str
    provenance_note: str
    approved: bool
    sort_keys: tuple[RankingSortKey, ...]

    def __post_init__(self) -> None:
        _identifier(self.score_version, "score_version", maximum=128)
        _identifier(self.provenance_note, "provenance_note", maximum=500)
        if not isinstance(self.approved, bool):
            raise TypeError("approved must be bool")
        if not self.sort_keys:
            raise ValueError("sort_keys must not be empty")
        cleaned: list[RankingSortKey] = []
        for key in self.sort_keys:
            if not isinstance(key, RankingSortKey):
                raise TypeError("sort_keys must contain RankingSortKey")
            cleaned.append(key)
        has_uid = any(
            key
            in (
                RankingSortKey.OPPORTUNITY_UID_ASC,
                RankingSortKey.OPPORTUNITY_UID_DESC,
            )
            for key in cleaned
        )
        if not has_uid:
            raise ValueError(
                "ranking policy must include an opportunity_uid sort key "
                "for deterministic tie-breaking"
            )
        object.__setattr__(self, "sort_keys", tuple(cleaned))


@dataclass(frozen=True)
class ConfidenceBand:
    """Diagnostic confidence label band — not a win-rate or probability."""

    min_inclusive: Decimal
    max_exclusive: Decimal
    label: str

    def __post_init__(self) -> None:
        lo = _decimal(self.min_inclusive, "min_inclusive")
        hi = _decimal(self.max_exclusive, "max_exclusive")
        if lo < Decimal("0") or hi > Decimal("1") or lo >= hi:
            raise ValueError("confidence band bounds invalid")
        object.__setattr__(self, "min_inclusive", lo)
        object.__setattr__(self, "max_exclusive", hi)
        _identifier(self.label, "label", maximum=64)


@dataclass(frozen=True)
class ConfidencePolicy:
    """Confidence label bands for a score version — labels only."""

    score_version: str
    provenance_note: str
    approved: bool
    bands: tuple[ConfidenceBand, ...]

    def __post_init__(self) -> None:
        _identifier(self.score_version, "score_version", maximum=128)
        _identifier(self.provenance_note, "provenance_note", maximum=500)
        if not isinstance(self.approved, bool):
            raise TypeError("approved must be bool")
        if not self.bands:
            raise ValueError("confidence bands must not be empty")
        ordered = sorted(self.bands, key=lambda b: b.min_inclusive)
        if ordered[0].min_inclusive != Decimal("0"):
            raise ValueError("confidence bands must start at 0")
        cursor = Decimal("0")
        for band in ordered:
            if band.min_inclusive != cursor:
                raise ValueError("confidence bands must be contiguous without gaps")
            cursor = band.max_exclusive
        if cursor != Decimal("1"):
            # Final band may use max_exclusive=1; scores of exactly 1 map to last band.
            raise ValueError("confidence bands must end at 1")
        object.__setattr__(self, "bands", tuple(ordered))

    def label_for(self, score: Decimal) -> str:
        if score == Decimal("1"):
            last = self.bands[-1]
            return last.label
        for band in self.bands:
            if band.min_inclusive <= score < band.max_exclusive:
                return band.label
        raise ValueError("score outside confidence bands")


@dataclass(frozen=True)
class ScoringPolicy:
    """Complete approved scoring policy bundle for one score version."""

    score_version: str
    provenance_note: str
    approved: bool
    weights: WeightPolicy
    thresholds: ThresholdPolicy
    ranking: RankingPolicy
    confidence: ConfidencePolicy

    def __post_init__(self) -> None:
        _identifier(self.score_version, "score_version", maximum=128)
        _identifier(self.provenance_note, "provenance_note", maximum=500)
        if not isinstance(self.approved, bool):
            raise TypeError("approved must be bool")
        for field_name, part in (
            ("weights", self.weights),
            ("thresholds", self.thresholds),
            ("ranking", self.ranking),
            ("confidence", self.confidence),
        ):
            if part is None:
                raise TypeError(f"{field_name} is required")


@dataclass(frozen=True)
class ComponentContribution:
    """Explainable weighted contribution for one component."""

    component_id: ScoreComponentId
    present: bool
    weight: Decimal
    contribution: Decimal
    diagnostic: str


@dataclass(frozen=True)
class ScoredOpportunity:
    """Scored opportunity under an explicit policy version."""

    opportunity_uid: str
    strategy_kind: str
    score_version: str
    score: Decimal
    confidence_label: str
    as_of: datetime
    contributions: tuple[ComponentContribution, ...]
    policy_provenance: str

    def __post_init__(self) -> None:
        _identifier(self.opportunity_uid, "opportunity_uid", maximum=256)
        _identifier(self.strategy_kind, "strategy_kind", maximum=64)
        _identifier(self.score_version, "score_version", maximum=128)
        _decimal(self.score, "score")
        _identifier(self.confidence_label, "confidence_label", maximum=64)
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        _identifier(self.policy_provenance, "policy_provenance", maximum=500)


def default_scoring_policy() -> None:
    """V4.10 ships no default weights/thresholds/ranking/confidence."""

    return None


def validate_scoring_policy(policy: ScoringPolicy | None) -> PolicyReject | None:
    """Validate completeness, approval, and provenance alignment."""
    if policy is None:
        return PolicyReject(
            code=PolicyRejectCode.MISSING_POLICY,
            message=(
                "scoring/ranking unsupported: no ScoringPolicy supplied "
                "(V4.10 does not invent default weights, thresholds, or ranking)"
            ),
        )
    if not isinstance(policy, ScoringPolicy):
        return PolicyReject(
            code=PolicyRejectCode.INCOMPLETE_POLICY,
            message="scoring policy must be a ScoringPolicy instance",
        )
    if not policy.approved:
        return PolicyReject(
            code=PolicyRejectCode.POLICY_NOT_APPROVED,
            message="scoring policy is not approved",
            score_version=policy.score_version,
        )
    parts = (
        ("weights", policy.weights, PolicyRejectCode.MISSING_WEIGHT_POLICY),
        (
            "thresholds",
            policy.thresholds,
            PolicyRejectCode.MISSING_THRESHOLD_POLICY,
        ),
        ("ranking", policy.ranking, PolicyRejectCode.MISSING_RANKING_POLICY),
        (
            "confidence",
            policy.confidence,
            PolicyRejectCode.MISSING_CONFIDENCE_POLICY,
        ),
    )
    for name, part, missing_code in parts:
        if part is None:
            return PolicyReject(
                code=missing_code,
                message=f"scoring policy missing {name}",
                score_version=policy.score_version,
            )
        if not getattr(part, "approved", False):
            return PolicyReject(
                code=PolicyRejectCode.POLICY_NOT_APPROVED,
                message=f"{name} policy is not approved",
                score_version=policy.score_version,
            )
        if part.score_version != policy.score_version:
            return PolicyReject(
                code=PolicyRejectCode.CONTRADICTORY_POLICY,
                message=(
                    f"{name} score_version {part.score_version!r} does not match "
                    f"bundle score_version {policy.score_version!r}"
                ),
                score_version=policy.score_version,
            )
        if not part.provenance_note:
            return PolicyReject(
                code=PolicyRejectCode.INCOMPLETE_POLICY,
                message=f"{name} provenance_note is required",
                score_version=policy.score_version,
            )
    if not policy.provenance_note:
        return PolicyReject(
            code=PolicyRejectCode.INCOMPLETE_POLICY,
            message="scoring policy provenance_note is required",
            score_version=policy.score_version,
        )
    # Structural invariants already enforced in dataclass __post_init__;
    # re-check weight coverage for required components.
    weight_ids = {cid for cid, _ in policy.weights.component_weights}
    for required in policy.weights.required_components:
        if required not in weight_ids:
            return PolicyReject(
                code=PolicyRejectCode.INCOMPLETE_POLICY,
                message=f"required component {required.value} lacks a weight",
                score_version=policy.score_version,
            )
    return None


def _policy_to_score_reject(
    reject: PolicyReject, request: UnifiedScoreRequest | None = None
) -> ScoreReject:
    mapping = {
        PolicyRejectCode.MISSING_POLICY: ScoreRejectCode.MISSING_WEIGHT_POLICY,
        PolicyRejectCode.MISSING_WEIGHT_POLICY: ScoreRejectCode.MISSING_WEIGHT_POLICY,
        PolicyRejectCode.MISSING_THRESHOLD_POLICY: ScoreRejectCode.MISSING_THRESHOLD_POLICY,
        PolicyRejectCode.MISSING_RANKING_POLICY: ScoreRejectCode.MISSING_RANKING_POLICY,
        PolicyRejectCode.MISSING_CONFIDENCE_POLICY: ScoreRejectCode.MISSING_RANKING_POLICY,
        PolicyRejectCode.POLICY_NOT_APPROVED: ScoreRejectCode.SCORE_UNSUPPORTED,
        PolicyRejectCode.INCOMPLETE_POLICY: ScoreRejectCode.SCORE_UNSUPPORTED,
        PolicyRejectCode.CONTRADICTORY_POLICY: ScoreRejectCode.CONTRADICTORY_EVIDENCE,
        PolicyRejectCode.INVALID_SCORE_VERSION: ScoreRejectCode.INVALID_SCORE_REQUEST,
        PolicyRejectCode.MIXED_SCORE_VERSION: ScoreRejectCode.INVALID_SCORE_REQUEST,
        PolicyRejectCode.BELOW_THRESHOLD: ScoreRejectCode.SCORE_UNSUPPORTED,
        PolicyRejectCode.RANKING_UNSUPPORTED: ScoreRejectCode.MISSING_RANKING_POLICY,
    }
    return ScoreReject(
        code=mapping.get(reject.code, ScoreRejectCode.SCORE_UNSUPPORTED),
        message=reject.message,
        opportunity_uid=request.opportunity_uid if request else None,
        strategy_kind=request.strategy_kind.value if request else None,
    )


def score_with_policy(
    request: UnifiedScoreRequest,
    policy: ScoringPolicy | None,
) -> tuple[ScoredOpportunity | None, ScoreReject | None]:
    """Score one opportunity under a complete approved policy.

    Component contributions use policy weights × presence (1/0). Does not invent
    component magnitudes, probabilities, or ROI.
    """
    if not isinstance(request, UnifiedScoreRequest):
        raise TypeError("request must be UnifiedScoreRequest")
    policy_err = validate_scoring_policy(policy)
    if policy_err is not None:
        return None, _policy_to_score_reject(policy_err, request)
    assert policy is not None

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

    by_id: dict[ScoreComponentId, ScoreComponent] = {
        c.component_id: c for c in request.components
    }
    for required in policy.weights.required_components:
        component = by_id.get(required)
        if component is None:
            return None, ScoreReject(
                code=ScoreRejectCode.MISSING_REQUIRED_INPUT,
                message=f"required score component missing: {required.value}",
                opportunity_uid=request.opportunity_uid,
                strategy_kind=request.strategy_kind.value,
            )
        if not component.present:
            return None, ScoreReject(
                code=ScoreRejectCode.MISSING_REQUIRED_INPUT,
                message=f"required score component not present: {required.value}",
                opportunity_uid=request.opportunity_uid,
                strategy_kind=request.strategy_kind.value,
            )

    contributions: list[ComponentContribution] = []
    total = Decimal("0")
    for component_id, weight in policy.weights.component_weights:
        component = by_id.get(component_id)
        present = bool(component and component.present)
        contribution = weight if present else Decimal("0")
        total += contribution
        diagnostic = (
            component.diagnostic
            if component is not None
            else f"{component_id.value} absent from request"
        )
        contributions.append(
            ComponentContribution(
                component_id=component_id,
                present=present,
                weight=weight,
                contribution=contribution,
                diagnostic=diagnostic,
            )
        )

    if total < policy.thresholds.inclusion_min:
        return None, ScoreReject(
            code=ScoreRejectCode.SCORE_UNSUPPORTED,
            message=(
                f"score {total} below inclusion_min "
                f"{policy.thresholds.inclusion_min} "
                f"(policy {policy.score_version})"
            ),
            opportunity_uid=request.opportunity_uid,
            strategy_kind=request.strategy_kind.value,
        )

    try:
        label = policy.confidence.label_for(total)
    except ValueError as exc:
        return None, ScoreReject(
            code=ScoreRejectCode.INVALID_SCORE_REQUEST,
            message=str(exc),
            opportunity_uid=request.opportunity_uid,
            strategy_kind=request.strategy_kind.value,
        )

    scored = ScoredOpportunity(
        opportunity_uid=request.opportunity_uid,
        strategy_kind=request.strategy_kind.value,
        score_version=policy.score_version,
        score=total,
        confidence_label=label,
        as_of=request.as_of,
        contributions=tuple(contributions),
        policy_provenance=policy.provenance_note,
    )
    return scored, None


def _sort_tuple(item: ScoredOpportunity, keys: tuple[RankingSortKey, ...]) -> tuple:
    parts: list[object] = []
    for key in keys:
        if key is RankingSortKey.SCORE_DESC:
            parts.append(-item.score)
        elif key is RankingSortKey.SCORE_ASC:
            parts.append(item.score)
        elif key is RankingSortKey.AS_OF_DESC:
            parts.append(-item.as_of.timestamp())
        elif key is RankingSortKey.AS_OF_ASC:
            parts.append(item.as_of.timestamp())
        elif key is RankingSortKey.OPPORTUNITY_UID_ASC:
            parts.append(item.opportunity_uid)
        elif key is RankingSortKey.OPPORTUNITY_UID_DESC:
            parts.append(tuple(-ord(c) for c in item.opportunity_uid))
        else:
            raise ValueError(f"unsupported ranking key {key}")
    return tuple(parts)


def rank_scored_opportunities(
    scored: tuple[ScoredOpportunity, ...] | list[ScoredOpportunity],
    policy: ScoringPolicy | None,
) -> tuple[tuple[ScoredOpportunity, ...] | None, PolicyReject | None]:
    """Deterministic ranking only under a complete approved policy."""
    policy_err = validate_scoring_policy(policy)
    if policy_err is not None:
        return None, policy_err
    assert policy is not None

    items = list(scored)
    if not items:
        return (), None

    versions = {item.score_version for item in items}
    if versions != {policy.score_version}:
        return None, PolicyReject(
            code=PolicyRejectCode.MIXED_SCORE_VERSION,
            message=(
                "ranking rejected: scored items must share the policy "
                f"score_version {policy.score_version!r}"
            ),
            score_version=policy.score_version,
        )

    ranked = sorted(items, key=lambda item: _sort_tuple(item, policy.ranking.sort_keys))
    return tuple(ranked), None


def format_scored_opportunity_card(scored: ScoredOpportunity) -> str:
    """Honest scored card — confidence label is not a probability."""
    lines = [
        "UNIFIED SCORE (policy-enabled)",
        f"Opportunity: {scored.opportunity_uid}",
        f"Strategy: {scored.strategy_kind}",
        f"Score version: {scored.score_version}",
        f"Score: {scored.score}",
        f"Confidence label: {scored.confidence_label} (not a probability)",
        f"As of: {scored.as_of.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Policy: {scored.policy_provenance}",
        "",
        "Explainable contributions:",
    ]
    for item in scored.contributions:
        flag = "present" if item.present else "missing"
        lines.append(
            f"- {item.component_id.value}: {flag} weight={item.weight} "
            f"contrib={item.contribution} — {item.diagnostic}"
        )
    lines.extend(
        [
            "",
            "Public read-only / paper-only. Not guaranteed profit.",
            "Score is not a probability, win-rate, or ROI claim.",
        ]
    )
    return "\n".join(lines)
