"""Explainable remaining-maps / late-game probability. No LLM, no invented calibration."""

from __future__ import annotations

from decimal import Decimal
from math import comb

from app.opportunity_intel.live_edge.models import (
    EvidenceTier,
    LiveStateSnapshot,
    ProbabilityRange,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
# Remaining undecided maps: no skill assumed; slight under-50 keeps the bound conservative.
UNINFORMED_MAP_P = Decimal("0.48")
UNINFORMED_MAP_P_HIGH = Decimal("0.52")


def series_win_probability(
    maps_won: tuple[int, int],
    *,
    best_of: int,
    current_map_p: ProbabilityRange | None,
) -> tuple[ProbabilityRange, EvidenceTier] | None:
    """P(leader wins series) from remaining-map combinatorics.

    Undecided future maps use an uninformed conservative p=0.48, not a fake 95% table.
    A live current-map probability, when present, is applied only to the in-progress map.
    """
    wins_needed = best_of // 2 + 1
    lead, trail = maps_won
    if lead < 0 or trail < 0 or lead + trail >= best_of and lead < wins_needed and trail < wins_needed:
        if lead + trail > best_of:
            return None
    a_need = wins_needed - lead
    b_need = wins_needed - trail
    if a_need <= 0 and b_need <= 0:
        return None
    if a_need <= 0:
        return ProbabilityRange(Decimal("0.99"), Decimal("0.99")), EvidenceTier.HIGH
    if b_need <= 0:
        return None
    if current_map_p is None:
        low = _series_from_need(a_need, b_need, UNINFORMED_MAP_P)
        high = _series_from_need(a_need, b_need, UNINFORMED_MAP_P_HIGH)
        tier = EvidenceTier.MEDIUM if a_need == 1 and b_need >= 2 else EvidenceTier.LOW
        return _range(low, high), tier
    first_low = current_map_p.low
    first_high = current_map_p.high
    low = first_low * _ONE + (_ONE - first_low) * _series_from_need(
        a_need, b_need - 1, UNINFORMED_MAP_P
    )
    if b_need - 1 <= 0:
        low = first_low
    high = first_high * _ONE + (_ONE - first_high) * _series_from_need(
        a_need, b_need - 1, UNINFORMED_MAP_P_HIGH
    )
    if b_need - 1 <= 0:
        high = first_high
    tier = (
        EvidenceTier.HIGH
        if current_map_p.low >= Decimal("0.80") and a_need == 1
        else EvidenceTier.MEDIUM
    )
    return _range(low, high), tier


def map_win_probability(state: LiveStateSnapshot) -> tuple[ProbabilityRange, EvidenceTier] | None:
    """Conservative current-map win range from agreeing live metrics only."""
    supporting = 0
    late = _is_late(state)
    strongest = _ZERO
    reasons = 0
    if state.kills is not None:
        diff = state.kills[0] - state.kills[1]
        if diff >= 10:
            supporting += 1
            strongest = max(strongest, Decimal("0.72"))
            reasons += 1
        elif diff >= 5:
            supporting += 1
            strongest = max(strongest, Decimal("0.58"))
        elif diff <= -3:
            return None
    if state.gold_difference is not None:
        if state.gold_difference >= 8000:
            supporting += 1
            strongest = max(strongest, Decimal("0.74"))
            reasons += 1
        elif state.gold_difference >= 4000:
            supporting += 1
            strongest = max(strongest, Decimal("0.60"))
        elif state.gold_difference <= -4000:
            return None
    if state.net_worth_difference is not None:
        if state.net_worth_difference >= 15000:
            supporting += 1
            strongest = max(strongest, Decimal("0.74"))
            reasons += 1
        elif state.net_worth_difference >= 8000:
            supporting += 1
            strongest = max(strongest, Decimal("0.60"))
        elif state.net_worth_difference <= -8000:
            return None
    if state.round_score is not None:
        a, b = state.round_score
        if a - b >= 8 or (a >= 11 and a - b >= 5):
            supporting += 1
            strongest = max(strongest, Decimal("0.78"))
            reasons += 1
        elif a - b >= 4:
            supporting += 1
            strongest = max(strongest, Decimal("0.62"))
        elif a <= b:
            return None
    if state.towers is not None:
        tower_diff = state.towers[0] - state.towers[1]
        if tower_diff >= 5:
            supporting += 1
            strongest = max(strongest, Decimal("0.76"))
            reasons += 1
        elif tower_diff >= 3:
            supporting += 1
            strongest = max(strongest, Decimal("0.60"))
        elif tower_diff <= -3:
            return None
    if supporting == 0 or strongest <= _ZERO:
        return None
    if late:
        strongest = min(Decimal("0.96"), strongest + Decimal("0.12"))
    low = min(Decimal("0.96"), max(Decimal("0.55"), strongest - Decimal("0.08")))
    high = min(Decimal("0.98"), low + Decimal("0.06"))
    if late and reasons >= 2 and low >= Decimal("0.80"):
        tier = EvidenceTier.HIGH
    elif supporting >= 2 and low >= Decimal("0.62"):
        tier = EvidenceTier.MEDIUM
    else:
        tier = EvidenceTier.LOW
    return ProbabilityRange(low, high), tier


def traditional_sport_probability(
    sport: str, state: LiveStateSnapshot
) -> tuple[ProbabilityRange, EvidenceTier] | None:
    """High-confidence only when score AND remaining-time/context agree. No score-only 99%."""
    if sport == "tennis":
        if state.sets is None:
            return None
        diff = state.sets[0] - state.sets[1]
        if diff < 1:
            return None
        games_lead = 0
        if state.games is not None:
            games_lead = state.games[0] - state.games[1]
        if diff >= 1 and games_lead >= 3:
            return (
                ProbabilityRange(Decimal("0.80"), Decimal("0.90")),
                EvidenceTier.MEDIUM,
            )
        if diff >= 2:
            return (
                ProbabilityRange(Decimal("0.88"), Decimal("0.94")),
                EvidenceTier.HIGH,
            )
        return None
    if sport == "basketball":
        if state.score is None or state.remaining_seconds is None:
            return None
        diff = state.score[0] - state.score[1]
        remaining = state.remaining_seconds
        if remaining <= 120 and diff >= 18:
            return ProbabilityRange(Decimal("0.90"), Decimal("0.96")), EvidenceTier.HIGH
        if remaining <= 180 and diff >= 15:
            return ProbabilityRange(Decimal("0.86"), Decimal("0.93")), EvidenceTier.MEDIUM
        return None
    if sport == "baseball":
        if state.score is None or state.inning is None:
            return None
        diff = state.score[0] - state.score[1]
        if state.inning >= 8 and diff >= 5:
            return ProbabilityRange(Decimal("0.86"), Decimal("0.94")), EvidenceTier.HIGH
        if state.inning >= 8 and diff >= 3:
            return ProbabilityRange(Decimal("0.78"), Decimal("0.88")), EvidenceTier.MEDIUM
        return None
    if sport == "soccer":
        if state.score is None or state.minute is None:
            return None
        diff = state.score[0] - state.score[1]
        if state.minute < 80:
            return None
        red_ok = state.red_cards is None or state.red_cards[1] >= state.red_cards[0]
        if diff >= 3 and red_ok and state.minute >= 80:
            return ProbabilityRange(Decimal("0.84"), Decimal("0.92")), EvidenceTier.HIGH
        if diff >= 2 and red_ok and state.minute >= 88:
            return ProbabilityRange(Decimal("0.78"), Decimal("0.88")), EvidenceTier.MEDIUM
        return None
    return None


def _series_from_need(a_need: int, b_need: int, p_map: Decimal) -> Decimal:
    if a_need <= 0:
        return _ONE
    if b_need <= 0:
        return _ZERO
    p = float(p_map)
    q = 1.0 - p
    total = 0.0
    # A wins on exactly the trial where they collect a_need wins, with B still below b_need.
    for losses in range(0, b_need):
        trials_before = a_need - 1 + losses
        total += comb(trials_before, losses) * (p ** a_need) * (q ** losses)
    return Decimal(str(round(total, 6)))


def _range(low: Decimal, high: Decimal) -> ProbabilityRange:
    low = min(_ONE, max(_ZERO, low))
    high = min(_ONE, max(low, high))
    return ProbabilityRange(low, high)


def _is_late(state: LiveStateSnapshot) -> bool:
    if state.game_time_seconds is not None and state.game_time_seconds >= 25 * 60:
        return True
    if state.round_score is not None:
        a, b = state.round_score
        if a >= 11 or b >= 11:
            return True
    if state.inhibitors is not None and state.inhibitors[0] >= 1:
        return True
    return False
