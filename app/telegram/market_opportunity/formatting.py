"""Telegram copy for Market Opportunities (V4.2 WTI admin UX)."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

PAPER_DISCLAIMER = (
    "Paper-only. No real orders. Ideas are not guaranteed profit. "
    "You make the final decision."
)

EIA_ATTRIBUTION = "Source: U.S. Energy Information Administration."
FRED_NOTICE = (
    "This product uses the FRED® API but is not endorsed or certified "
    "by the Federal Reserve Bank of St. Louis."
)

HUB_TEXT = (
    "📊 Market Opportunities\n"
    "\n"
    "V4.2 WTI Opportunity Intelligence (admin-only).\n"
    "Primary instrument: EIA WTI Cushing Spot — Daily (RWTC).\n"
    "Gold detectors deferred — no licensed price feed.\n"
    "No auto alerts. Confidence: LOW / MEDIUM / HIGH only "
    "(not a predicted win rate).\n"
    "\n"
    f"{EIA_ATTRIBUTION}\n"
    f"{FRED_NOTICE}\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

TOP_EMPTY_TEXT = (
    "📊 Top Opportunities\n"
    "\n"
    "No published paper opportunities yet.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

WTI_EMPTY_TEXT = (
    "🛢 WTI Opportunities\n"
    "\n"
    "No published WTI paper opportunities yet.\n"
    "Instrument: EIA WTI Cushing Spot — Daily (RWTC).\n"
    "High/low equal close on daily EIA prints.\n"
    "\n"
    f"{EIA_ATTRIBUTION}\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

GOLD_STUB_TEXT = (
    "🥇 Gold\n"
    "\n"
    "Deferred in V4.2 — no licensed or ToS-cleared Gold price feed.\n"
    "Runnable Gold detectors are not registered.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

DETAIL_NOT_FOUND_TEXT = (
    "📄 Opportunity detail\n"
    "\n"
    "Opportunity not found.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

EVIDENCE_EMPTY_TEXT = (
    "🔎 Evidence\n"
    "\n"
    "No evidence rows for this opportunity.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

RISKS_EMPTY_TEXT = (
    "⚠️ Risks & counterarguments\n"
    "\n"
    "No published opportunity selected.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

HISTORY_PLACEHOLDER_TEXT = (
    "🕘 Paper history\n"
    "\n"
    "Open and measured paper outcomes appear as opportunities are "
    "published and measured.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

STATS_PLACEHOLDER_TEXT = (
    "📈 Performance & calibration\n"
    "\n"
    "Placeholder — confidence calibration requires measured paper "
    "outcomes (V4.6).\n"
    "Until then, LOW / MEDIUM / HIGH means data completeness / "
    "history depth only.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

FILTERS_PLACEHOLDER_TEXT = (
    "🎛 Filters\n"
    "\n"
    "Placeholder — market / horizon / risk filters arrive with "
    "Top Opportunities scoring (V4.5).\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)


def format_opportunity_list(
    title: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    empty_text: str,
) -> str:
    if not rows:
        return empty_text
    lines = [title, ""]
    for row in rows[:20]:
        uid = str(row.get("opportunity_uid") or "")
        side = str(row.get("side") or "")
        conf = str(row.get("confidence") or "")
        otype = str(row.get("opportunity_type") or "")
        price = row.get("verified_price")
        lines.append(f"• {side} {otype} [{conf}]")
        lines.append(f"  uid={uid}")
        if price is not None:
            lines.append(f"  price={price}")
        lines.append("")
    lines.append(EIA_ATTRIBUTION)
    lines.append(PAPER_DISCLAIMER)
    return "\n".join(lines)


def format_opportunity_detail(row: Mapping[str, Any] | None) -> str:
    if not row:
        return DETAIL_NOT_FOUND_TEXT
    outcome = row.get("outcome") or {}
    lines = [
        "📄 Opportunity detail",
        "",
        f"uid: {row.get('opportunity_uid')}",
        f"type: {row.get('opportunity_type')}",
        f"side: {row.get('side')}",
        f"status: {row.get('status')}",
        f"confidence: {row.get('confidence')}",
        f"instrument: {row.get('display_name') or row.get('symbol')}",
        f"market_id: {row.get('market_id')}",
        f"verified_price: {row.get('verified_price')}",
        f"entry: [{row.get('entry_zone_low')}, {row.get('entry_zone_high')}]",
        f"invalidation: {row.get('invalidation_price')}",
        f"targets: {row.get('targets_json')}",
        f"reward/risk: {row.get('reward_risk')}",
        f"horizon: {row.get('time_horizon')}",
        f"expires_at: {row.get('expires_at')}",
        f"paper_only: {row.get('paper_only')}",
        "",
        "Paper outcome:",
        f"  entry_reached={outcome.get('entry_reached')}",
        f"  entry_missed={outcome.get('entry_missed')}",
        f"  invalidation_reached={outcome.get('invalidation_reached')}",
        f"  mfe={outcome.get('mfe')} mae={outcome.get('mae')}",
        f"  return_1d={outcome.get('return_1d')} return_5d={outcome.get('return_5d')}",
        "",
        EIA_ATTRIBUTION,
        FRED_NOTICE,
        PAPER_DISCLAIMER,
    ]
    return "\n".join(str(x) for x in lines)


def format_evidence(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return EVIDENCE_EMPTY_TEXT
    lines = ["🔎 Evidence", ""]
    for row in rows:
        lines.append(f"• {row.get('summary')}")
        lines.append(
            f"  source={row.get('source_id')} kind={row.get('series_kind')} "
            f"event={row.get('source_event_at')} fetched={row.get('fetched_at')}"
        )
        lines.append("")
    lines.append(EIA_ATTRIBUTION)
    lines.append(PAPER_DISCLAIMER)
    return "\n".join(lines)


def format_risks(counterarguments: Sequence[str]) -> str:
    if not counterarguments:
        return RISKS_EMPTY_TEXT
    lines = ["⚠️ Risks & counterarguments", ""]
    for item in counterarguments:
        lines.append(f"• {item}")
    lines.append("")
    lines.append(PAPER_DISCLAIMER)
    return "\n".join(lines)
