"""Telegram copy for Market Opportunities (V4.1 admin shell)."""

from __future__ import annotations

PAPER_DISCLAIMER = (
    "Paper-only. No real orders. Ideas are not guaranteed profit. "
    "You make the final decision."
)

HUB_TEXT = (
    "📊 Market Opportunities\n"
    "\n"
    "V4.1 foundation (admin-only).\n"
    "No live detectors. No auto alerts.\n"
    "Confidence bands: LOW / MEDIUM / HIGH only "
    "(not a predicted win rate).\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

TOP_EMPTY_TEXT = (
    "📊 Top Opportunities\n"
    "\n"
    "No published paper opportunities yet.\n"
    "Live detectors begin in later V4 versions "
    "(V4.2: Gold + WTI).\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

DETAIL_NOT_FOUND_TEXT = (
    "📄 Opportunity detail\n"
    "\n"
    "Opportunity not found.\n"
    "V4.1 has no published ideas.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

EVIDENCE_EMPTY_TEXT = (
    "🔎 Evidence\n"
    "\n"
    "No evidence rows yet.\n"
    "When ideas publish, each fact will include source provenance "
    "and timestamps.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

RISKS_EMPTY_TEXT = (
    "⚠️ Risks & counterarguments\n"
    "\n"
    "No published opportunity selected.\n"
    "Every published idea must include counterarguments and risks.\n"
    "\n"
    f"{PAPER_DISCLAIMER}"
)

HISTORY_PLACEHOLDER_TEXT = (
    "🕘 Paper history\n"
    "\n"
    "Placeholder — outcome tracking starts with published ideas "
    "(from V4.2 onward).\n"
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
