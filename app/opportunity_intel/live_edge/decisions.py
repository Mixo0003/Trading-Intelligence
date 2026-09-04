"""BUY / WAIT / TOO LATE / SKIP — fail closed, no invented probability."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.opportunity_intel.live_edge.economics import (
    conservative_edge_pp,
    expected_value_usd,
)
from app.opportunity_intel.live_edge.models import (
    EvidenceTier,
    ExecutableQuote,
    LiveEdgeCategory,
    LiveEdgeDecision,
    LiveEdgeDecisionResult,
    LiveEdgeEvaluation,
    LiveEdgeSignalType,
    MatchStatus,
    SkipReason,
    WaitTrigger,
)
from app.opportunity_intel.live_edge.policy import (
    CALCULATION_MAX_AGE_SECONDS,
    LIVE_STATE_MAX_AGE_BY_PROVIDER,
    LIVE_STATE_MAX_AGE_SECONDS,
    MINIMUM_CONSERVATIVE_EDGE_PP,
    MINIMUM_EXPECTED_ROI,
    NEAR_RESOLUTION_MINIMUM_EDGE_PP,
    PREFERRED_CONSERVATIVE_EDGE_PP,
    PREFERRED_EXPECTED_ROI,
    QUOTE_MAX_AGE_SECONDS,
    TOO_LATE_EDGE_CEILING_PP,
    TOO_LATE_PRICE_FLOOR,
    TOO_LATE_PROBABILITY_FLOOR,
    WEATHER_EVIDENCE_MAX_AGE_SECONDS,
)

_ZERO = Decimal("0")


def _min_edge(evaluation: LiveEdgeEvaluation) -> Decimal:
    if evaluation.near_resolution or (
        evaluation.signal_type == LiveEdgeSignalType.NEAR_RESOLUTION_EDGE
    ):
        return NEAR_RESOLUTION_MINIMUM_EDGE_PP
    return MINIMUM_CONSERVATIVE_EDGE_PP


def _max_live_age(evaluation: LiveEdgeEvaluation) -> int:
    provider_name = (
        evaluation.provenance.provider if evaluation.provenance is not None else ""
    )
    if evaluation.identity.category == LiveEdgeCategory.WEATHER:
        return LIVE_STATE_MAX_AGE_BY_PROVIDER.get(
            provider_name, WEATHER_EVIDENCE_MAX_AGE_SECONDS
        )
    return LIVE_STATE_MAX_AGE_BY_PROVIDER.get(provider_name, LIVE_STATE_MAX_AGE_SECONDS)


def decide(
    evaluation: LiveEdgeEvaluation,
    quote: ExecutableQuote | None,
    *,
    now: datetime | None = None,
    known_fees_usd: Decimal = _ZERO,
) -> LiveEdgeDecisionResult:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    calc_age = (clock - evaluation.evaluated_at.astimezone(timezone.utc)).total_seconds()
    if calc_age > CALCULATION_MAX_AGE_SECONDS:
        return _skip(
            evaluation,
            quote,
            SkipReason.STALE_QUOTE,
            clock,
            ("розрахунок застарів",),
        )
    if _is_hard_skip_reason(evaluation.skip_reason):
        return _skip(
            evaluation, quote, evaluation.skip_reason, clock, evaluation.reasons
        )
    if evaluation.unresolved_conditions:
        return _skip(
            evaluation,
            quote,
            SkipReason.UNRESOLVED_CONDITION,
            clock,
            evaluation.unresolved_conditions,
        )
    if evaluation.match.status == MatchStatus.AMBIGUOUS:
        return _skip(
            evaluation, quote, SkipReason.AMBIGUOUS_MATCH, clock, evaluation.reasons
        )
    if evaluation.match.status == MatchStatus.NO_MATCH:
        return _skip(evaluation, quote, SkipReason.NO_MATCH, clock, evaluation.reasons)
    if evaluation.match.status == MatchStatus.PROBABLE_MATCH:
        max_live_age = _max_live_age(evaluation)
        if (
            evaluation.live_data_age_seconds is not None
            and evaluation.live_data_age_seconds > Decimal(max_live_age)
        ):
            return _skip(
                evaluation,
                quote,
                SkipReason.STALE_LIVE_DATA,
                clock,
                ("live-дані застаріли",),
            )
        if evaluation.live_state is not None:
            return _watch(evaluation, quote, clock)
        return _skip(
            evaluation,
            quote,
            SkipReason.MATCH_NOT_VERIFIED,
            clock,
            ("PROBABLE_MATCH без live-стану — без WATCH",),
        )
    if evaluation.match.status != MatchStatus.VERIFIED_MATCH:
        return _skip(
            evaluation,
            quote,
            SkipReason.MATCH_NOT_VERIFIED,
            clock,
            ("для сигналу потрібен VERIFIED_MATCH",),
        )
    max_live_age = _max_live_age(evaluation)
    if (
        evaluation.live_data_age_seconds is not None
        and evaluation.live_data_age_seconds > Decimal(max_live_age)
    ):
        return _skip(
            evaluation,
            quote,
            SkipReason.STALE_LIVE_DATA,
            clock,
            ("live-дані застаріли",),
        )
    if _should_watch(evaluation):
        return _watch(evaluation, quote, clock)
    if _should_wait_medium(evaluation):
        return _wait_medium(evaluation, quote, clock)
    if evaluation.probability is None:
        return _skip(
            evaluation,
            quote,
            SkipReason.INSUFFICIENT_DATA,
            clock,
            ("немає перевіреної ймовірності",),
        )
    if evaluation.evidence_tier != EvidenceTier.HIGH:
        return _skip(
            evaluation,
            quote,
            SkipReason.LOW_EVIDENCE,
            clock,
            ("рівень evidence недостатній для BUY",),
        )
    if quote is None:
        return _skip(
            evaluation,
            quote,
            SkipReason.INSUFFICIENT_LIQUIDITY,
            clock,
            ("немає executable котирування",),
        )
    if quote.quote_age_seconds > Decimal(QUOTE_MAX_AGE_SECONDS):
        return _skip(
            evaluation, quote, SkipReason.STALE_QUOTE, clock, ("котирування застаріло",)
        )
    if not quote.fully_executable:
        trigger = WaitTrigger(
            description_uk=(
                f"Зайти, якщо ліквідність стане достатньою для ${quote.allocation_usd}"
            ),
            require_executable_bankroll=True,
        )
        return LiveEdgeDecisionResult(
            evaluation=evaluation,
            decision=LiveEdgeDecision.WAIT,
            side=quote.side,
            quote=quote,
            edge_pp=None,
            expected_value_usd=None,
            expected_roi=None,
            wait_trigger=trigger,
            skip_reason=SkipReason.BANKROLL_NOT_EXECUTABLE,
            decision_reasons=("ліквідність недостатня для обраного капіталу",),
            calculated_at=clock,
        )

    edge = conservative_edge_pp(evaluation.probability, quote)
    ev_pair = expected_value_usd(
        probability=evaluation.probability,
        quote=quote,
        known_fees_usd=known_fees_usd,
    )
    min_edge = _min_edge(evaluation)
    price = quote.vwap
    p_con = evaluation.probability.conservative

    if (
        price >= Decimal("0.995")
        and edge < TOO_LATE_EDGE_CEILING_PP
    ) or (
        price >= Decimal("0.995")
        and p_con >= Decimal("0.90")
        and edge < TOO_LATE_EDGE_CEILING_PP
    ) or (
        p_con >= TOO_LATE_PROBABILITY_FLOOR
        and price >= TOO_LATE_PRICE_FLOOR
        and edge < TOO_LATE_EDGE_CEILING_PP
    ):
        return LiveEdgeDecisionResult(
            evaluation=evaluation,
            decision=LiveEdgeDecision.TOO_LATE,
            side=quote.side,
            quote=quote,
            edge_pp=edge,
            expected_value_usd=ev_pair[0] if ev_pair else None,
            expected_roi=ev_pair[1] if ev_pair else None,
            wait_trigger=None,
            skip_reason=None,
            decision_reasons=(
                "результат уже майже повністю врахований у ціні Polymarket, "
                "тому потенційна вигода занадто мала",
            ),
            calculated_at=clock,
        )

    if ev_pair is None or ev_pair[0] <= _ZERO:
        return _skip(
            evaluation,
            quote,
            SkipReason.NEGATIVE_EV,
            clock,
            ("очікувана вигода не є додатною",),
            edge=edge,
            ev_pair=ev_pair,
        )
    ev_usd, roi = ev_pair
    if roi < MINIMUM_EXPECTED_ROI:
        return _skip(
            evaluation,
            quote,
            SkipReason.NEGATIVE_EV,
            clock,
            ("очікувана дохідність нижча за мінімум",),
            edge=edge,
            ev_pair=ev_pair,
        )
    if edge < min_edge:
        if edge > _ZERO:
            target_ask = p_con - PREFERRED_CONSERVATIVE_EDGE_PP
            if target_ask <= _ZERO:
                target_ask = p_con - min_edge
            trigger = WaitTrigger(
                description_uk=(
                    f"Зайти, якщо {quote.side} ask ≤ {target_ask.quantize(Decimal('0.01'))} "
                    f"або перевага стане ≥ {int(PREFERRED_CONSERVATIVE_EDGE_PP * 100)} п.п."
                ),
                max_yes_ask=target_ask if quote.side == "YES" else None,
                min_edge_pp=PREFERRED_CONSERVATIVE_EDGE_PP,
            )
            return LiveEdgeDecisionResult(
                evaluation=evaluation,
                decision=LiveEdgeDecision.WAIT,
                side=quote.side,
                quote=quote,
                edge_pp=edge,
                expected_value_usd=ev_usd,
                expected_roi=roi,
                wait_trigger=trigger,
                skip_reason=None,
                decision_reasons=(
                    "сильна фактична перевага, але поточна ціна ще не відповідає вимогам",
                ),
                calculated_at=clock,
            )
        return _skip(
            evaluation,
            quote,
            SkipReason.EDGE_TOO_SMALL,
            clock,
            ("перевага занадто мала",),
            edge=edge,
            ev_pair=ev_pair,
        )

    if (
        edge >= PREFERRED_CONSERVATIVE_EDGE_PP
        and roi >= PREFERRED_EXPECTED_ROI
        and evaluation.provenance is not None
        and evaluation.evidence_tier == EvidenceTier.HIGH
    ):
        return LiveEdgeDecisionResult(
            evaluation=evaluation,
            decision=LiveEdgeDecision.BUY_NOW,
            side=quote.side,
            quote=quote,
            edge_pp=edge,
            expected_value_usd=ev_usd,
            expected_roi=roi,
            wait_trigger=None,
            skip_reason=None,
            decision_reasons=evaluation.reasons,
            calculated_at=clock,
        )
    if (
        edge >= min_edge
        and roi >= MINIMUM_EXPECTED_ROI
        and evaluation.provenance is not None
        and evaluation.evidence_tier == EvidenceTier.HIGH
    ):
        return LiveEdgeDecisionResult(
            evaluation=evaluation,
            decision=LiveEdgeDecision.BUY_NOW,
            side=quote.side,
            quote=quote,
            edge_pp=edge,
            expected_value_usd=ev_usd,
            expected_roi=roi,
            wait_trigger=None,
            skip_reason=None,
            decision_reasons=evaluation.reasons,
            calculated_at=clock,
        )
    if (
        evaluation.evidence_tier == EvidenceTier.MEDIUM
        and edge > _ZERO
        and evaluation.provenance is not None
    ):
        target_ask = p_con - PREFERRED_CONSERVATIVE_EDGE_PP
        trigger = WaitTrigger(
            description_uk=(
                f"Зайти, якщо {quote.side} ask ≤ {target_ask.quantize(Decimal('0.01'))} "
                f"та evidence стане HIGH"
            ),
            max_yes_ask=target_ask if quote.side == "YES" else None,
            min_edge_pp=PREFERRED_CONSERVATIVE_EDGE_PP,
        )
        return LiveEdgeDecisionResult(
            evaluation=evaluation,
            decision=LiveEdgeDecision.WAIT,
            side=quote.side,
            quote=quote,
            edge_pp=edge,
            expected_value_usd=ev_usd,
            expected_roi=roi,
            wait_trigger=trigger,
            skip_reason=None,
            decision_reasons=("evidence MEDIUM — чекати сильнішого підтвердження",),
            calculated_at=clock,
        )
    return _skip(
        evaluation,
        quote,
        SkipReason.UNRELIABLE_SOURCE,
        clock,
        ("немає перевіреного джерела для входу",),
        edge=edge,
        ev_pair=ev_pair,
    )


_HARD_SKIP_REASONS: frozenset[SkipReason] = frozenset(
    {
        SkipReason.SOURCE_CONFLICT,
        SkipReason.WRONG_MAP,
        SkipReason.MAP_ALREADY_FINISHED,
        SkipReason.STALE_LIVE_DATA,
        SkipReason.STALE_QUOTE,
        SkipReason.AMBIGUOUS_MATCH,
        SkipReason.NO_MATCH,
        SkipReason.MATCH_NOT_VERIFIED,
        SkipReason.UNSUPPORTED_CATEGORY,
        SkipReason.UNRELIABLE_SOURCE,
        SkipReason.INVENTED_PROBABILITY_FORBIDDEN,
        SkipReason.MARKET_FINISHED,
        SkipReason.UNRESOLVED_CONDITION,
        SkipReason.AMBIGUOUS_RESOLUTION,
        SkipReason.STALE_EXTERNAL_EVIDENCE,
        SkipReason.NEGATIVE_EV,
        SkipReason.FEATURE_OFF,
    }
)


def _is_hard_skip_reason(reason: SkipReason | None) -> bool:
    return reason is not None and reason in _HARD_SKIP_REASONS


def _watchable_live(evaluation: LiveEdgeEvaluation) -> bool:
    return evaluation.live_state is not None


def _live_or_near(evaluation: LiveEdgeEvaluation) -> bool:
    if evaluation.near_resolution:
        return True
    if evaluation.market.live is True:
        return True
    if evaluation.live_state is not None:
        return True
    return evaluation.signal_type in {
        LiveEdgeSignalType.LIVE_MAP_EDGE,
        LiveEdgeSignalType.LIVE_MATCH_EDGE,
        LiveEdgeSignalType.NEAR_RESOLUTION_EDGE,
    }


def _should_watch(evaluation: LiveEdgeEvaluation) -> bool:
    if not _watchable_live(evaluation):
        return False
    if evaluation.evidence_tier == EvidenceTier.LOW:
        return True
    if evaluation.probability is None and evaluation.evidence_tier != EvidenceTier.MEDIUM:
        return True
    return False


def _should_wait_medium(evaluation: LiveEdgeEvaluation) -> bool:
    if evaluation.evidence_tier != EvidenceTier.MEDIUM:
        return False
    if not _live_or_near(evaluation):
        return False
    return evaluation.provenance is not None or evaluation.live_state is not None


def _watch(
    evaluation: LiveEdgeEvaluation,
    quote: ExecutableQuote | None,
    clock: datetime,
) -> LiveEdgeDecisionResult:
    return LiveEdgeDecisionResult(
        evaluation=evaluation,
        decision=LiveEdgeDecision.WATCH,
        side=quote.side if quote is not None else "YES",
        quote=quote,
        edge_pp=None,
        expected_value_usd=None,
        expected_roi=None,
        wait_trigger=None,
        skip_reason=None,
        decision_reasons=(
            "WATCH / LIVE CANDIDATE — не рекомендація входу",
            *evaluation.reasons,
        ),
        calculated_at=clock,
    )


def _wait_medium(
    evaluation: LiveEdgeEvaluation,
    quote: ExecutableQuote | None,
    clock: datetime,
) -> LiveEdgeDecisionResult:
    trigger = WaitTrigger(
        description_uk=(
            "Зайти, якщо evidence стане HIGH, з'явиться executable quote "
            "і перевага/EV будуть додатними"
        ),
        require_executable_bankroll=True,
        min_edge_pp=PREFERRED_CONSERVATIVE_EDGE_PP,
    )
    if (
        quote is not None
        and evaluation.probability is not None
        and quote.fully_executable
    ):
        p_con = evaluation.probability.conservative
        target_ask = p_con - PREFERRED_CONSERVATIVE_EDGE_PP
        trigger = WaitTrigger(
            description_uk=(
                f"Зайти, якщо {quote.side} ask ≤ {target_ask.quantize(Decimal('0.01'))} "
                f"та evidence стане HIGH"
            ),
            max_yes_ask=target_ask if quote.side == "YES" else None,
            min_edge_pp=PREFERRED_CONSERVATIVE_EDGE_PP,
            require_executable_bankroll=True,
        )
    return LiveEdgeDecisionResult(
        evaluation=evaluation,
        decision=LiveEdgeDecision.WAIT,
        side=quote.side if quote is not None else "YES",
        quote=quote,
        edge_pp=None,
        expected_value_usd=None,
        expected_roi=None,
        wait_trigger=trigger,
        skip_reason=None,
        decision_reasons=("evidence MEDIUM — чекати HIGH confirmation і executable входу",),
        calculated_at=clock,
    )


def wait_trigger_met(
    trigger: WaitTrigger,
    *,
    quote: ExecutableQuote | None,
    edge_pp: Decimal | None,
    current_map: int | None,
) -> bool:
    if trigger.require_executable_bankroll:
        if quote is None or not quote.fully_executable:
            return False
    if trigger.max_yes_ask is not None:
        if quote is None or quote.side != "YES" or quote.vwap > trigger.max_yes_ask:
            if quote is None or quote.best_ask > trigger.max_yes_ask:
                return False
    if trigger.min_edge_pp is not None:
        if edge_pp is None or edge_pp < trigger.min_edge_pp:
            return False
    if trigger.required_map_win is not None:
        if current_map is None or current_map < trigger.required_map_win:
            return False
    return True


def _skip(
    evaluation: LiveEdgeEvaluation,
    quote: ExecutableQuote | None,
    reason: SkipReason,
    clock: datetime,
    reasons: tuple[str, ...],
    *,
    edge: Decimal | None = None,
    ev_pair: tuple[Decimal, Decimal] | None = None,
) -> LiveEdgeDecisionResult:
    return LiveEdgeDecisionResult(
        evaluation=evaluation,
        decision=LiveEdgeDecision.SKIP,
        side=quote.side if quote is not None else "YES",
        quote=quote,
        edge_pp=edge,
        expected_value_usd=ev_pair[0] if ev_pair else None,
        expected_roi=ev_pair[1] if ev_pair else None,
        wait_trigger=None,
        skip_reason=reason,
        decision_reasons=reasons,
        calculated_at=clock,
    )
