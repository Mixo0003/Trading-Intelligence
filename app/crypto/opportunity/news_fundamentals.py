"""V4.7 news and fundamental intelligence foundation (contracts + validation).

Integrates news/fundamental evidence as overlays on **existing** opportunities
only. News alone must never create a trading signal.

Supported event-class **labels** (no invented ingest adapters in this slice):
official economic releases; central banks / CPI / FOMC; EIA / OPEC; earnings
and company guidance; regulatory events; relevant geopolitics; asset/venue
trading rules and specifications.

Every evidence item must carry URL, publisher/source type, published and
retrieved timestamps, related asset/instrument, deduplication key, verification
status, and provenance.

Does **not** invent source registries, TTLs, ranking, confidence, or
profitability formulas. Evaluation and background passes fail closed until
approved policies exist. Does not add Telegram News buttons or change spot
arbitrage / directional foundation modules.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from urllib.parse import urlparse

from app.crypto.product_types import ProductType
from app.crypto.venue_registry import CanonicalInstrument, V43_EXCHANGE_IDS


class NewsEventClass(StrEnum):
    OFFICIAL_ECONOMIC_RELEASE = "official_economic_release"
    CENTRAL_BANK_CPI_FOMC = "central_bank_cpi_fomc"
    EIA_OPEC = "eia_opec"
    EARNINGS_GUIDANCE = "earnings_guidance"
    REGULATORY = "regulatory"
    GEOPOLITICS = "geopolitics"
    ASSET_VENUE_TRADING_RULES = "asset_venue_trading_rules"


class NewsSourceType(StrEnum):
    OFFICIAL = "official"
    REGULATORY = "regulatory"
    EXCHANGE = "exchange"
    COMPANY = "company"
    WIRE = "wire"
    OTHER = "other"


class NewsVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class NewsOverlayAction(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    DOWNGRADE = "downgrade"
    SUPPRESS = "suppress"


class NewsFundamentalRejectCode(StrEnum):
    MISSING_SOURCE_REGISTRY = "missing_source_registry"
    MISSING_FRESHNESS_POLICY = "missing_freshness_policy"
    MISSING_RANKING_POLICY = "missing_ranking_policy"
    MISSING_OPPORTUNITY_REF = "missing_opportunity_ref"
    NEWS_ALONE_SIGNAL_FORBIDDEN = "news_alone_signal_forbidden"
    UNVERIFIED_EVIDENCE = "unverified_evidence"
    STALE_EVIDENCE = "stale_evidence"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    DUPLICATE_EVIDENCE = "duplicate_evidence"
    IRRELEVANT_EVIDENCE = "irrelevant_evidence"
    AMBIGUOUS_ASSET_LABEL = "ambiguous_asset_label"
    UNSUPPORTED_VENUE = "unsupported_venue"
    UNSUPPORTED_PRODUCT = "unsupported_product"
    MISSING_ASSET_RULES = "missing_asset_rules"
    INVALID_EVIDENCE = "invalid_evidence"
    UNSUPPORTED_OVERLAY = "unsupported_overlay"


@dataclass(frozen=True)
class NewsFundamentalReject:
    code: NewsFundamentalRejectCode
    message: str
    opportunity_uid: str | None = None
    evidence_id: str | None = None
    dedupe_key: str | None = None


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


def _require_http_url(url: str) -> str:
    text = _identifier(url, "url", maximum=2048)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    return text


# Ambiguous commodity labels must never be treated as verified underlyings.
_AMBIGUOUS_ASSET_LABELS = frozenset(
    {
        "gold",
        "oil",
        "crude",
        "wti",
        "brent",
        "silver",
        "natgas",
        "natural gas",
    }
)


@dataclass(frozen=True)
class NewsProvenance:
    source_id: str
    publisher: str
    source_type: NewsSourceType
    url: str
    published_at: datetime
    retrieved_at: datetime
    note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id", maximum=128)
        _identifier(self.publisher, "publisher", maximum=256)
        if not isinstance(self.source_type, NewsSourceType):
            raise TypeError("source_type must be NewsSourceType")
        object.__setattr__(self, "url", _require_http_url(self.url))
        object.__setattr__(
            self, "published_at", _utc(self.published_at, "published_at")
        )
        object.__setattr__(
            self, "retrieved_at", _utc(self.retrieved_at, "retrieved_at")
        )
        if self.retrieved_at < self.published_at:
            raise ValueError("retrieved_at must not be before published_at")
        if self.note:
            _identifier(self.note, "note", maximum=500)


@dataclass(frozen=True)
class AssetRuleContext:
    """Declared venue/product constraints for relevance checks (labels only).

    Does not invent fee, leverage, or liquidity formulas. Missing required
    fields fail closed when validation is requested.
    """

    venue_id: str
    product_type: ProductType
    settlement: str | None = None
    contract_size: str | None = None
    trading_hours: str | None = None
    leverage: str | None = None
    liquidity_note: str | None = None
    fee_note: str | None = None
    restrictions: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.venue_id, "venue_id", maximum=64)
        if not isinstance(self.product_type, ProductType):
            raise TypeError("product_type must be ProductType")
        for name in (
            "settlement",
            "contract_size",
            "trading_hours",
            "leverage",
            "liquidity_note",
            "fee_note",
            "restrictions",
        ):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name, maximum=256)


@dataclass(frozen=True)
class NewsEvidenceItem:
    """Normalized news/fundamental evidence with full provenance."""

    evidence_id: str
    event_class: NewsEventClass
    related_asset_label: str
    related_instrument: CanonicalInstrument | None
    dedupe_key: str
    verification_status: NewsVerificationStatus
    provenance: NewsProvenance
    summary: str
    asset_rules: AssetRuleContext | None = None

    def __post_init__(self) -> None:
        _identifier(self.evidence_id, "evidence_id", maximum=256)
        if not isinstance(self.event_class, NewsEventClass):
            raise TypeError("event_class must be NewsEventClass")
        label = _identifier(self.related_asset_label, "related_asset_label", maximum=128)
        object.__setattr__(self, "related_asset_label", label)
        _identifier(self.dedupe_key, "dedupe_key", maximum=512)
        if not isinstance(self.verification_status, NewsVerificationStatus):
            raise TypeError("verification_status must be NewsVerificationStatus")
        if not isinstance(self.provenance, NewsProvenance):
            raise TypeError("provenance must be NewsProvenance")
        _identifier(self.summary, "summary", maximum=1000)
        if self.related_instrument is not None and not isinstance(
            self.related_instrument, CanonicalInstrument
        ):
            raise TypeError("related_instrument must be CanonicalInstrument")
        if self.asset_rules is not None and not isinstance(
            self.asset_rules, AssetRuleContext
        ):
            raise TypeError("asset_rules must be AssetRuleContext")
        if label.strip().lower() in _AMBIGUOUS_ASSET_LABELS:
            raise ValueError(
                "ambiguous asset label rejected "
                "(never assume Gold/Oil/etc. identify the underlying)"
            )


@dataclass(frozen=True)
class OpportunityNewsOverlay:
    """News overlay applied to an existing opportunity only."""

    opportunity_uid: str
    action: NewsOverlayAction
    evidence: NewsEvidenceItem
    as_of: datetime

    def __post_init__(self) -> None:
        _identifier(self.opportunity_uid, "opportunity_uid", maximum=256)
        if not isinstance(self.action, NewsOverlayAction):
            raise TypeError("action must be NewsOverlayAction")
        if not isinstance(self.evidence, NewsEvidenceItem):
            raise TypeError("evidence must be NewsEvidenceItem")
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))


def validate_news_evidence_item(item: NewsEvidenceItem) -> NewsFundamentalReject | None:
    """Reject unverified / contradictory / irrelevant evidence at the contract layer."""
    if item.verification_status is NewsVerificationStatus.UNVERIFIED:
        return NewsFundamentalReject(
            code=NewsFundamentalRejectCode.UNVERIFIED_EVIDENCE,
            message="evidence verification_status is unverified",
            evidence_id=item.evidence_id,
            dedupe_key=item.dedupe_key,
        )
    if item.verification_status is NewsVerificationStatus.REJECTED:
        return NewsFundamentalReject(
            code=NewsFundamentalRejectCode.UNVERIFIED_EVIDENCE,
            message="evidence verification_status is rejected",
            evidence_id=item.evidence_id,
            dedupe_key=item.dedupe_key,
        )
    if item.related_instrument is None and item.event_class is not (
        NewsEventClass.ASSET_VENUE_TRADING_RULES
    ):
        # Trading-rules events may be venue-scoped via asset_rules only; others
        # require a concrete instrument identity to avoid label-only mapping.
        if item.asset_rules is None:
            return NewsFundamentalReject(
                code=NewsFundamentalRejectCode.IRRELEVANT_EVIDENCE,
                message=(
                    "evidence lacks related_instrument and asset_rules; "
                    "cannot map event to a verified asset"
                ),
                evidence_id=item.evidence_id,
                dedupe_key=item.dedupe_key,
            )
    if item.related_instrument is not None:
        venue = item.related_instrument.venue_id
        if venue not in V43_EXCHANGE_IDS:
            return NewsFundamentalReject(
                code=NewsFundamentalRejectCode.UNSUPPORTED_VENUE,
                message=(
                    f"venue {venue!r} is not in Market Intelligence V4 venues "
                    "(Bybit remains legacy V3 only)"
                ),
                evidence_id=item.evidence_id,
            )
        if item.related_instrument.product_type != ProductType.SPOT:
            return NewsFundamentalReject(
                code=NewsFundamentalRejectCode.UNSUPPORTED_PRODUCT,
                message=(
                    "futures-related news overlay unsupported without verified "
                    "futures adapters"
                ),
                evidence_id=item.evidence_id,
            )
    if item.asset_rules is not None and item.asset_rules.venue_id not in V43_EXCHANGE_IDS:
        return NewsFundamentalReject(
            code=NewsFundamentalRejectCode.UNSUPPORTED_VENUE,
            message=f"asset_rules venue {item.asset_rules.venue_id!r} not in V4 venues",
            evidence_id=item.evidence_id,
        )
    return None


def dedupe_news_evidence(
    items: tuple[NewsEvidenceItem, ...],
) -> tuple[tuple[NewsEvidenceItem, ...], tuple[NewsFundamentalReject, ...]]:
    """Keep first occurrence per dedupe_key; reject duplicates."""
    kept: list[NewsEvidenceItem] = []
    rejects: list[NewsFundamentalReject] = []
    seen: set[str] = set()
    for item in items:
        if item.dedupe_key in seen:
            rejects.append(
                NewsFundamentalReject(
                    code=NewsFundamentalRejectCode.DUPLICATE_EVIDENCE,
                    message=f"duplicate dedupe_key {item.dedupe_key!r}",
                    evidence_id=item.evidence_id,
                    dedupe_key=item.dedupe_key,
                )
            )
            continue
        seen.add(item.dedupe_key)
        kept.append(item)
    return tuple(kept), tuple(rejects)


def reject_contradictory_overlays(
    overlays: tuple[OpportunityNewsOverlay, ...],
) -> NewsFundamentalReject | None:
    """Same opportunity+evidence cannot both confirm and reject."""
    by_pair: dict[tuple[str, str], set[NewsOverlayAction]] = {}
    for overlay in overlays:
        key = (overlay.opportunity_uid, overlay.evidence.dedupe_key)
        by_pair.setdefault(key, set()).add(overlay.action)
    for (uid, dedupe), actions in by_pair.items():
        if (
            NewsOverlayAction.CONFIRM in actions
            and NewsOverlayAction.REJECT in actions
        ):
            return NewsFundamentalReject(
                code=NewsFundamentalRejectCode.CONTRADICTORY_EVIDENCE,
                message=(
                    "contradictory confirm+reject overlays for the same "
                    "opportunity and evidence"
                ),
                opportunity_uid=uid,
                dedupe_key=dedupe,
            )
    return None


def create_signal_from_news_alone(
    evidence: NewsEvidenceItem,
) -> tuple[None, NewsFundamentalReject]:
    """Hard rule: news alone never creates a trading signal."""
    if not isinstance(evidence, NewsEvidenceItem):
        raise TypeError("evidence must be NewsEvidenceItem")
    return None, NewsFundamentalReject(
        code=NewsFundamentalRejectCode.NEWS_ALONE_SIGNAL_FORBIDDEN,
        message=(
            "news/fundamental evidence alone must never create a trading signal"
        ),
        evidence_id=evidence.evidence_id,
        dedupe_key=evidence.dedupe_key,
    )


def evaluate_news_fundamental_overlay(
    overlay: OpportunityNewsOverlay,
    *,
    source_registry: object | None = None,
    freshness_policy: object | None = None,
    ranking_policy: object | None = None,
    require_ranking: bool = False,
) -> tuple[None, NewsFundamentalReject]:
    """Apply news to an existing opportunity — fail closed without policies.

    Never invents ranking/confidence/profitability. Background integration is
    unsupported until an approved source registry exists.
    """
    if not isinstance(overlay, OpportunityNewsOverlay):
        raise TypeError("overlay must be OpportunityNewsOverlay")
    if not overlay.opportunity_uid:
        return None, NewsFundamentalReject(
            code=NewsFundamentalRejectCode.MISSING_OPPORTUNITY_REF,
            message="overlay requires an existing opportunity_uid",
        )

    evidence_reject = validate_news_evidence_item(overlay.evidence)
    if evidence_reject is not None:
        return None, NewsFundamentalReject(
            code=evidence_reject.code,
            message=evidence_reject.message,
            opportunity_uid=overlay.opportunity_uid,
            evidence_id=evidence_reject.evidence_id,
            dedupe_key=evidence_reject.dedupe_key,
        )

    if overlay.evidence.event_class is NewsEventClass.ASSET_VENUE_TRADING_RULES:
        if overlay.evidence.asset_rules is None:
            return None, NewsFundamentalReject(
                code=NewsFundamentalRejectCode.MISSING_ASSET_RULES,
                message=(
                    "asset/venue trading-rules evidence requires asset_rules "
                    "context"
                ),
                opportunity_uid=overlay.opportunity_uid,
                evidence_id=overlay.evidence.evidence_id,
            )

    if source_registry is None:
        return None, NewsFundamentalReject(
            code=NewsFundamentalRejectCode.MISSING_SOURCE_REGISTRY,
            message=(
                "news overlay unsupported: no approved authoritative source "
                "registry (V4.7 foundation does not invent publishers)"
            ),
            opportunity_uid=overlay.opportunity_uid,
            evidence_id=overlay.evidence.evidence_id,
        )
    if freshness_policy is None:
        return None, NewsFundamentalReject(
            code=NewsFundamentalRejectCode.MISSING_FRESHNESS_POLICY,
            message=(
                "news overlay unsupported: no approved freshness policy "
                "(V4.7 foundation does not invent TTLs)"
            ),
            opportunity_uid=overlay.opportunity_uid,
            evidence_id=overlay.evidence.evidence_id,
        )
    if require_ranking and ranking_policy is None:
        return None, NewsFundamentalReject(
            code=NewsFundamentalRejectCode.MISSING_RANKING_POLICY,
            message=(
                "news overlay unsupported: ranking requested but no approved "
                "ranking policy (V4.7 foundation does not invent ranking)"
            ),
            opportunity_uid=overlay.opportunity_uid,
            evidence_id=overlay.evidence.evidence_id,
        )
    return None, NewsFundamentalReject(
        code=NewsFundamentalRejectCode.UNSUPPORTED_OVERLAY,
        message=(
            f"news {overlay.action.value} overlay unsupported: complete "
            "news/fundamental engine is not implemented in V4.7 foundation"
        ),
        opportunity_uid=overlay.opportunity_uid,
        evidence_id=overlay.evidence.evidence_id,
        dedupe_key=overlay.evidence.dedupe_key,
    )


def run_background_news_integration(
    *,
    existing_opportunity_uids: tuple[str, ...],
    evidence_batch: tuple[NewsEvidenceItem, ...],
    source_registry: object | None = None,
    freshness_policy: object | None = None,
) -> tuple[None, NewsFundamentalReject]:
    """Automatic background pass — fail closed without approved sources.

    Does not create opportunities. Does not register Telegram jobs here.
    """
    if not existing_opportunity_uids:
        return None, NewsFundamentalReject(
            code=NewsFundamentalRejectCode.MISSING_OPPORTUNITY_REF,
            message=(
                "background news integration requires existing opportunities; "
                "news alone cannot create signals"
            ),
        )
    if source_registry is None:
        return None, NewsFundamentalReject(
            code=NewsFundamentalRejectCode.MISSING_SOURCE_REGISTRY,
            message=(
                "background news integration unsupported: no approved source "
                "registry"
            ),
        )
    if freshness_policy is None:
        return None, NewsFundamentalReject(
            code=NewsFundamentalRejectCode.MISSING_FRESHNESS_POLICY,
            message=(
                "background news integration unsupported: no approved "
                "freshness policy"
            ),
        )
    _, dupes = dedupe_news_evidence(evidence_batch)
    if dupes:
        return None, dupes[0]
    return None, NewsFundamentalReject(
        code=NewsFundamentalRejectCode.UNSUPPORTED_OVERLAY,
        message=(
            "background news/fundamental integration unsupported: complete "
            "engine not implemented in V4.7 foundation"
        ),
    )


def format_news_fundamental_status_card(
    overlay: OpportunityNewsOverlay,
    reject: NewsFundamentalReject,
) -> str:
    """Honest paper/read-only status card for news overlays."""
    ev = overlay.evidence
    lines = [
        "NEWS / FUNDAMENTAL OVERLAY (foundation)",
        f"Action: {overlay.action.value}",
        f"Existing opportunity: {overlay.opportunity_uid}",
        f"Event class: {ev.event_class.value}",
        f"Related asset label: {ev.related_asset_label}",
        f"Verification: {ev.verification_status.value}",
        f"Publisher: {ev.provenance.publisher} ({ev.provenance.source_type.value})",
        f"URL: {ev.provenance.url}",
        f"Published: {ev.provenance.published_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Retrieved: {ev.provenance.retrieved_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Dedupe key: {ev.dedupe_key}",
        f"Summary: {ev.summary}",
        "",
        f"Status: unsupported ({reject.code.value})",
        reject.message,
        "",
        "Public read-only / paper-only. Not guaranteed profit.",
        "News alone never creates a trading signal.",
        "No separate News button. No invented ranking or confidence.",
    ]
    return "\n".join(lines)
