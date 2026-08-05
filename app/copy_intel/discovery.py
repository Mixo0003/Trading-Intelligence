"""Automatic Polymarket trader discovery and ranking."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from app.config.settings import Settings
from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.data_client import CopyDataClient, normalize_wallet
from app.copy_intel.discovery_metrics import compute_discovery_metrics
from app.copy_intel.discovery_scoring import score_discovery_trader
from app.copy_intel.models import (
    CopyUserSettings,
    DiscoveryCandidate,
    DiscoveryScore,
    DiscoverySource,
)
from app.copy_intel.settings_profiles import get_user_settings
from app.database import copy_storage

logger = logging.getLogger(__name__)

# Polymarket Data API category enums (case-insensitive on API; use documented form).
DEFAULT_CATEGORIES = ("SPORTS", "POLITICS", "CRYPTO", "FINANCE")

DIAG_API_UNAVAILABLE = "api_unavailable"
DIAG_MALFORMED = "malformed_response"
DIAG_ZERO_CANDIDATES = "zero_candidates"
DIAG_REJECTED_BY_SCORING = "rejected_by_scoring"
DIAG_OK = "ok"


class DiscoveryError(Exception):
    """User-facing discovery failure that must not look like an empty trader list."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DiscoveryRunResult:
    candidates: list[DiscoveryCandidate]
    scores: list[DiscoveryScore]
    diagnostic_code: str = DIAG_OK
    diagnostic_message: str = ""


def dedupe_candidates(candidates: Iterable[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
    merged: dict[str, DiscoveryCandidate] = {}
    for cand in candidates:
        try:
            wallet = normalize_wallet(cand.wallet)
        except ValueError:
            continue
        existing = merged.get(wallet)
        if existing is None:
            merged[wallet] = DiscoveryCandidate(
                wallet=wallet,
                label=cand.label,
                sources=tuple(sorted(set(cand.sources))),
                leaderboard_pnl=cand.leaderboard_pnl,
                leaderboard_vol=cand.leaderboard_vol,
                category=cand.category,
            )
            continue
        sources = tuple(sorted(set(existing.sources) | set(cand.sources)))
        label = existing.label or cand.label
        pnl = existing.leaderboard_pnl
        if cand.leaderboard_pnl is not None:
            pnl = max(pnl or cand.leaderboard_pnl, cand.leaderboard_pnl)
        vol = existing.leaderboard_vol
        if cand.leaderboard_vol is not None:
            vol = max(vol or cand.leaderboard_vol, cand.leaderboard_vol)
        merged[wallet] = DiscoveryCandidate(
            wallet=wallet,
            label=label,
            sources=sources,
            leaderboard_pnl=pnl,
            leaderboard_vol=vol,
            category=existing.category or cand.category,
        )
    return list(merged.values())


def _leaderboard_query(
    client: CopyDataClient,
    *,
    page_size: int,
    max_pages: int,
    time_period: str | None,
    order_by: str | None,
    category: str | None = None,
) -> ApiSuccess[list[dict]] | ApiFailure:
    result = client.fetch_leaderboard_pages(
        page_size=page_size,
        max_pages=max_pages,
        time_period=time_period,
        order_by=order_by,
        category=category,
    )
    # Compatibility: tests may still monkeypatch a bare list return.
    if isinstance(result, list):
        return ApiSuccess(data=result)
    return result


def discover_candidates(
    settings: Settings,
    client: CopyDataClient | None = None,
    *,
    page_size: int = 25,
    max_pages: int = 2,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> list[DiscoveryCandidate]:
    """Discover candidates. Raises DiscoveryError on total API/malformed failure."""
    outcome = discover_candidates_result(
        settings,
        client=client,
        page_size=page_size,
        max_pages=max_pages,
        categories=categories,
    )
    if outcome.diagnostic_code in {DIAG_API_UNAVAILABLE, DIAG_MALFORMED}:
        raise DiscoveryError(outcome.diagnostic_code, outcome.diagnostic_message)
    return outcome.candidates


@dataclass
class _CandidateFetchOutcome:
    candidates: list[DiscoveryCandidate]
    diagnostic_code: str
    diagnostic_message: str
    queries_ok: int
    queries_failed: int
    raw_row_count: int
    valid_wallet_count: int


def discover_candidates_result(
    settings: Settings,
    client: CopyDataClient | None = None,
    *,
    page_size: int = 25,
    max_pages: int = 2,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> _CandidateFetchOutcome:
    client = client or CopyDataClient(settings)
    raw: list[DiscoveryCandidate] = []
    queries_ok = 0
    queries_failed = 0
    malformed = 0
    unavailable = 0
    raw_row_count = 0

    queries: list[tuple[str | None, str, DiscoverySource, str | None]] = [
        ("DAY", "VOL", "active", None),
        ("WEEK", "VOL", "active", None),
        ("WEEK", "PNL", "profitable", None),
        ("MONTH", "PNL", "profitable", None),
        ("ALL", "PNL", "profitable", None),
        ("DAY", "PNL", "rising", None),
    ]
    for category in categories:
        queries.append((None, "PNL", "category", category))

    for time_period, order_by, source, category in queries:
        result = _leaderboard_query(
            client,
            page_size=page_size if category is None else min(15, page_size),
            max_pages=max_pages if category is None else 1,
            time_period=time_period,
            order_by=order_by,
            category=category,
        )
        if isinstance(result, ApiFailure):
            queries_failed += 1
            if result.code == "leaderboard_malformed":
                malformed += 1
            else:
                unavailable += 1
            continue
        queries_ok += 1
        rows = result.data
        raw_row_count += len(rows)
        raw.extend(
            _rows_to_candidates(rows, source=source, category=category)  # type: ignore[arg-type]
        )

    if queries_ok == 0:
        if malformed and not unavailable:
            return _CandidateFetchOutcome(
                candidates=[],
                diagnostic_code=DIAG_MALFORMED,
                diagnostic_message=(
                    "Leaderboard API returned a malformed response. "
                    "Discovery cannot continue."
                ),
                queries_ok=0,
                queries_failed=queries_failed,
                raw_row_count=0,
                valid_wallet_count=0,
            )
        return _CandidateFetchOutcome(
            candidates=[],
            diagnostic_code=DIAG_API_UNAVAILABLE,
            diagnostic_message=(
                "Leaderboard API unavailable. "
                "Discovery cannot continue (not an empty trader list)."
            ),
            queries_ok=0,
            queries_failed=queries_failed,
            raw_row_count=0,
            valid_wallet_count=0,
        )

    candidates = dedupe_candidates(raw)
    if not candidates:
        return _CandidateFetchOutcome(
            candidates=[],
            diagnostic_code=DIAG_ZERO_CANDIDATES,
            diagnostic_message=(
                f"Leaderboard returned {raw_row_count} row(s) but no valid wallet "
                "addresses could be extracted."
            ),
            queries_ok=queries_ok,
            queries_failed=queries_failed,
            raw_row_count=raw_row_count,
            valid_wallet_count=0,
        )
    return _CandidateFetchOutcome(
        candidates=candidates,
        diagnostic_code=DIAG_OK,
        diagnostic_message="",
        queries_ok=queries_ok,
        queries_failed=queries_failed,
        raw_row_count=raw_row_count,
        valid_wallet_count=len(candidates),
    )


def analyze_candidates(
    settings: Settings,
    candidates: list[DiscoveryCandidate],
    user_settings: CopyUserSettings,
    client: CopyDataClient | None = None,
    *,
    limit: int = 20,
) -> list[DiscoveryScore]:
    client = client or CopyDataClient(settings)
    scores: list[DiscoveryScore] = []
    for cand in candidates[:limit]:
        try:
            metrics = compute_discovery_metrics(cand, client)
            score = score_discovery_trader(metrics, user_settings)
            scores.append(score)
            copy_storage.save_discovery_score(settings, score)
        except Exception:
            logger.exception("Discovery analysis failed for %s", cand.wallet[:10])
    scores.sort(key=lambda s: s.final_score, reverse=True)
    return scores


def run_discovery(
    settings: Settings,
    telegram_id: int = 0,
    *,
    analyze_limit: int = 12,
    client: CopyDataClient | None = None,
) -> tuple[list[DiscoveryCandidate], list[DiscoveryScore]]:
    """Backward-compatible entrypoint. Raises DiscoveryError on API/malformed failure."""
    result = run_discovery_detailed(
        settings,
        telegram_id=telegram_id,
        analyze_limit=analyze_limit,
        client=client,
    )
    if result.diagnostic_code in {DIAG_API_UNAVAILABLE, DIAG_MALFORMED}:
        raise DiscoveryError(result.diagnostic_code, result.diagnostic_message)
    return result.candidates, result.scores


def run_discovery_detailed(
    settings: Settings,
    telegram_id: int = 0,
    *,
    analyze_limit: int = 12,
    client: CopyDataClient | None = None,
) -> DiscoveryRunResult:
    client = client or CopyDataClient(settings)
    user_settings = get_user_settings(settings, telegram_id)
    fetch = discover_candidates_result(settings, client=client)
    if fetch.diagnostic_code in {DIAG_API_UNAVAILABLE, DIAG_MALFORMED}:
        copy_storage.set_poll_state(settings, "last_discovery_count", "0")
        copy_storage.set_poll_state(settings, "last_discovery_follow", "0")
        copy_storage.set_poll_state(
            settings, "last_discovery_diagnostic", fetch.diagnostic_code
        )
        return DiscoveryRunResult(
            candidates=[],
            scores=[],
            diagnostic_code=fetch.diagnostic_code,
            diagnostic_message=fetch.diagnostic_message,
        )
    if fetch.diagnostic_code == DIAG_ZERO_CANDIDATES:
        copy_storage.set_poll_state(settings, "last_discovery_count", "0")
        copy_storage.set_poll_state(settings, "last_discovery_follow", "0")
        copy_storage.set_poll_state(
            settings, "last_discovery_diagnostic", DIAG_ZERO_CANDIDATES
        )
        return DiscoveryRunResult(
            candidates=[],
            scores=[],
            diagnostic_code=DIAG_ZERO_CANDIDATES,
            diagnostic_message=fetch.diagnostic_message,
        )

    candidates = fetch.candidates
    scores = analyze_candidates(
        settings,
        candidates,
        user_settings,
        client=client,
        limit=analyze_limit,
    )
    copy_storage.set_poll_state(settings, "last_discovery_count", str(len(candidates)))
    copy_storage.set_poll_state(
        settings,
        "last_discovery_follow",
        str(sum(1 for s in scores if s.decision == "FOLLOW")),
    )
    follow_or_watch = [s for s in scores if s.decision in {"FOLLOW", "WATCH"}]
    if candidates and not follow_or_watch:
        msg = (
            f"Found {len(candidates)} candidate wallet(s); "
            f"analyzed {len(scores)} — none passed FOLLOW/WATCH scoring."
        )
        copy_storage.set_poll_state(
            settings, "last_discovery_diagnostic", DIAG_REJECTED_BY_SCORING
        )
        return DiscoveryRunResult(
            candidates=candidates,
            scores=scores,
            diagnostic_code=DIAG_REJECTED_BY_SCORING,
            diagnostic_message=msg,
        )
    copy_storage.set_poll_state(settings, "last_discovery_diagnostic", DIAG_OK)
    return DiscoveryRunResult(
        candidates=candidates,
        scores=scores,
        diagnostic_code=DIAG_OK,
        diagnostic_message="",
    )


def filter_scores(
    scores: list[DiscoveryScore],
    *,
    decision: str | None = None,
    rising_only: bool = False,
    high_risk_only: bool = False,
    category: str | None = None,
) -> list[DiscoveryScore]:
    out = scores
    if decision:
        out = [s for s in out if s.decision == decision]
    if high_risk_only:
        out = [s for s in out if s.risk_level == "High" or s.decision == "AVOID"]
    if rising_only:
        # Prefer higher recent vs lifetime ratio when available.
        out = sorted(
            out,
            key=lambda s: (
                (s.metrics.recent_pnl_30d or 0)
                / max(1.0, abs(s.metrics.lifetime_pnl or 1.0))
            ),
            reverse=True,
        )
    if category:
        cat = category.lower()
        out = [
            s
            for s in out
            if any(cat in c.lower() for c in s.metrics.main_categories)
        ]
    return out


def format_discovery_diagnostic(code: str, message: str) -> str:
    titles = {
        DIAG_API_UNAVAILABLE: "Trader Discovery — API unavailable",
        DIAG_MALFORMED: "Trader Discovery — malformed response",
        DIAG_ZERO_CANDIDATES: "Trader Discovery — no valid candidates",
        DIAG_REJECTED_BY_SCORING: "Trader Discovery — candidates rejected by scoring",
    }
    title = titles.get(code, "Trader Discovery failed")
    return f"{title}\n\n{message}"


def _rows_to_candidates(
    rows: list[dict],
    *,
    source: DiscoverySource,
    category: str | None = None,
) -> list[DiscoveryCandidate]:
    result: list[DiscoveryCandidate] = []
    for row in rows:
        wallet = (
            row.get("proxyWallet")
            or row.get("wallet")
            or row.get("user")
            or ""
        )
        try:
            wallet = normalize_wallet(str(wallet))
        except ValueError:
            continue
        label = row.get("userName") or row.get("xUsername") or None
        if label and str(label).startswith("0x") and len(str(label)) > 20:
            label = None
        pnl = row.get("pnl")
        vol = row.get("vol")
        result.append(
            DiscoveryCandidate(
                wallet=wallet,
                label=str(label) if label else None,
                sources=(source,),
                leaderboard_pnl=float(pnl) if pnl is not None else None,
                leaderboard_vol=float(vol) if vol is not None else None,
                category=category,
            )
        )
    return result
