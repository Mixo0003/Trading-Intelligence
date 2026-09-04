"""Bounded reason-code text for opportunity cards."""

from __future__ import annotations

from app.opportunity_intel.models import OpportunityReasonCode

REASON_TEXT: dict[OpportunityReasonCode, str] = {
    OpportunityReasonCode.FAST_5M_MOVE: "Fast 5-minute move",
    OpportunityReasonCode.SUSTAINED_15M_MOVE: "Sustained 15-minute move",
    OpportunityReasonCode.SUSTAINED_1H_MOVE: "Sustained 1-hour move",
    OpportunityReasonCode.HIGH_24H_VOLUME: "High 24h volume",
    OpportunityReasonCode.DEEP_EXECUTABLE_BOOK: "Deep executable book",
    OpportunityReasonCode.LOW_SLIPPAGE: "Low slippage",
    OpportunityReasonCode.TIGHT_SPREAD: "Tight spread",
    OpportunityReasonCode.LIVE_MARKET: "Live / in-play market",
    OpportunityReasonCode.LIVE_FAST_5M_MOVE: "Live fast 5-minute move",
    OpportunityReasonCode.LIVE_SUSTAINED_15M_MOVE: "Live sustained 15-minute move",
    OpportunityReasonCode.LIVE_SUSTAINED_1H_MOVE: "Live sustained 1-hour move",
    OpportunityReasonCode.LIVE_DEEP_EXECUTABLE_BOOK: "Live deep executable book",
    OpportunityReasonCode.LIVE_LOW_SLIPPAGE: "Live low slippage",
}

REASON_TEXT_UK: dict[OpportunityReasonCode, str] = {
    OpportunityReasonCode.FAST_5M_MOVE: "швидкий рух за 5 хвилин",
    OpportunityReasonCode.SUSTAINED_15M_MOVE: "стійкий рух за 15 хвилин",
    OpportunityReasonCode.SUSTAINED_1H_MOVE: "стійкий рух за 1 годину",
    OpportunityReasonCode.HIGH_24H_VOLUME: "високий обсяг за 24 години",
    OpportunityReasonCode.DEEP_EXECUTABLE_BOOK: "достатня глибина стакану",
    OpportunityReasonCode.LOW_SLIPPAGE: "низький slippage",
    OpportunityReasonCode.TIGHT_SPREAD: "вузький спред",
    OpportunityReasonCode.LIVE_MARKET: "live / in-play ринок",
    OpportunityReasonCode.LIVE_FAST_5M_MOVE: "швидкий live-рух за 5 хвилин",
    OpportunityReasonCode.LIVE_SUSTAINED_15M_MOVE: "стійкий live-рух за 15 хвилин",
    OpportunityReasonCode.LIVE_SUSTAINED_1H_MOVE: "стійкий live-рух за 1 годину",
    OpportunityReasonCode.LIVE_DEEP_EXECUTABLE_BOOK: "достатня live-глибина стакану",
    OpportunityReasonCode.LIVE_LOW_SLIPPAGE: "низький live slippage",
}


def reason_codes_to_text(codes: tuple[OpportunityReasonCode, ...], *, limit: int = 4) -> str:
    parts: list[str] = []
    for code in codes[:limit]:
        parts.append(REASON_TEXT.get(code, code.value.replace("_", " ").title()))
    return "; ".join(parts)


def reason_codes_to_uk_lines(
    codes: tuple[OpportunityReasonCode, ...], *, limit: int = 3
) -> tuple[str, ...]:
    lines: list[str] = []
    for code in codes[:limit]:
        lines.append(REASON_TEXT_UK.get(code, code.value.replace("_", " ").lower()))
    return tuple(lines)
