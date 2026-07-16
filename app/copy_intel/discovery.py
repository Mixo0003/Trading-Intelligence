"""Automatic Polymarket trader discovery and ranking."""

from __future__ import annotations

import logging
from typing import Iterable

from app.config.settings import Settings
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

DEFAULT_CATEGORIES = ("Sports", "Politics", "Crypto", "Finance")


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


def discover_candidates(
    settings: Settings,
    client: CopyDataClient | None = None,
    *,
    page_size: int = 25,
    max_pages: int = 2,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> list[DiscoveryCandidate]:
    client = client or CopyDataClient(settings)
    raw: list[DiscoveryCandidate] = []

    # Active traders (volume leaders, day/week).
    for period, source in (("DAY", "active"), ("WEEK", "active")):
        rows = client.fetch_leaderboard_pages(
            page_size=page_size,
            max_pages=max_pages,
            time_period=period,
            order_by="VOL",
        )
        raw.extend(_rows_to_candidates(rows, source=source))  # type: ignore[arg-type]

    # Profitable resolved-market / PnL leaders.
    for period in ("WEEK", "MONTH", None):
        rows = client.fetch_leaderboard_pages(
            page_size=page_size,
            max_pages=max_pages,
            time_period=period,
            order_by="PNL",
        )
        raw.extend(_rows_to_candidates(rows, source="profitable"))

    # Category leaders.
    for category in categories:
        rows = client.fetch_leaderboard_pages(
            page_size=min(15, page_size),
            max_pages=1,
            category=category,
            order_by="PNL",
        )
        raw.extend(_rows_to_candidates(rows, source="category", category=category))

    # Rising / recently active (day PnL).
    rows = client.fetch_leaderboard_pages(
        page_size=page_size,
        max_pages=1,
        time_period="DAY",
        order_by="PNL",
    )
    raw.extend(_rows_to_candidates(rows, source="rising"))

    return dedupe_candidates(raw)


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
    client = client or CopyDataClient(settings)
    user_settings = get_user_settings(settings, telegram_id)
    candidates = discover_candidates(settings, client=client)
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
    return candidates, scores


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


def _rows_to_candidates(
    rows: list[dict],
    *,
    source: DiscoverySource,
    category: str | None = None,
) -> list[DiscoveryCandidate]:
    result: list[DiscoveryCandidate] = []
    for row in rows:
        wallet = row.get("proxyWallet") or row.get("user") or ""
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
