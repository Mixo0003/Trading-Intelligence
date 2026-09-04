"""Category adapters. Unsupported or incomplete evidence fails closed."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from dataclasses import replace
from typing import Protocol

from app.opportunity_intel.live_edge.classify import is_near_resolution_candidate, signal_type_for
from app.opportunity_intel.live_edge.matching import align_snapshot_to_market, match_event
from app.opportunity_intel.live_edge.models import (
    DiscoveredMarket,
    EvidenceTier,
    LiveEdgeCategory,
    LiveEdgeEvaluation,
    LiveEdgeSignalType,
    LiveStateSnapshot,
    MarketIdentity,
    MarketKind,
    MatchStatus,
    ProbabilityRange,
    SkipReason,
)
from app.opportunity_intel.live_edge.probability import (
    map_win_probability,
    series_win_probability,
    traditional_sport_probability,
)
from app.opportunity_intel.live_edge.providers import LiveStateProvider, NullLiveStateProvider

_ZERO = Decimal("0")


class CategoryEvaluator(Protocol):
    category: LiveEdgeCategory

    def evaluate(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        snapshots: tuple[LiveStateSnapshot, ...],
        *,
        now: datetime,
    ) -> LiveEdgeEvaluation: ...


def _base_eval(
    market: DiscoveredMarket,
    identity: MarketIdentity,
    *,
    now: datetime,
    signal_type: LiveEdgeSignalType,
    match,
    probability: ProbabilityRange | None,
    reasons: tuple[str, ...],
    risks: tuple[str, ...],
    skip_reason: SkipReason | None,
    live_state: LiveStateSnapshot | None,
    unresolved: tuple[str, ...] = (),
    resolution_note: str | None = None,
    near_resolution: bool = False,
    evidence_tier: EvidenceTier = EvidenceTier.LOW,
) -> LiveEdgeEvaluation:
    remaining = None
    if market.end_at is not None:
        remaining = int((market.end_at - now).total_seconds())
    age = None
    provenance = live_state.provenance if live_state is not None else None
    if provenance is not None:
        age = provenance.age_seconds(now)
    return LiveEdgeEvaluation(
        market=market,
        identity=identity,
        signal_type=signal_type,
        match=match,
        probability=probability,
        reasons=reasons,
        risks=risks,
        skip_reason=skip_reason,
        live_state=live_state,
        provenance=provenance,
        unresolved_conditions=unresolved,
        estimated_end_in_seconds=remaining if remaining is not None and remaining >= 0 else None,
        estimated_resolution_note=resolution_note,
        evaluated_at=now,
        live_data_age_seconds=age,
        near_resolution=near_resolution,
        evidence_tier=evidence_tier,
    )


def _live_candidate(
    market: DiscoveredMarket,
    identity: MarketIdentity,
    *,
    now: datetime,
    match,
    live_state: LiveStateSnapshot,
    messages: tuple[str, ...],
    probability: ProbabilityRange | None = None,
    evidence_tier: EvidenceTier = EvidenceTier.LOW,
) -> LiveEdgeEvaluation:
    """Verified live event kept as a candidate. No invented probability. Not a BUY."""
    remaining = None
    if market.end_at is not None:
        remaining = int((market.end_at - now).total_seconds())
    signal_type = signal_type_for(
        identity, market, now_remaining_seconds=remaining, best_price_hint=_best_hint(market)
    )
    if identity.kind in {MarketKind.MAP_WINNER, MarketKind.GAME_WINNER}:
        signal_type = LiveEdgeSignalType.LIVE_MAP_EDGE
    elif identity.kind == MarketKind.SERIES_WINNER:
        signal_type = LiveEdgeSignalType.LIVE_MATCH_EDGE
    return _base_eval(
        market,
        identity,
        now=now,
        signal_type=signal_type,
        match=match,
        probability=probability,
        reasons=messages
        + ("live candidate — evidence ще недостатньо для BUY",),
        risks=("подія ще не завершена", "немає HIGH evidence для входу"),
        skip_reason=None,
        live_state=live_state,
        evidence_tier=evidence_tier,
    )


def _insufficient(
    market: DiscoveredMarket,
    identity: MarketIdentity,
    *,
    now: datetime,
    reason: SkipReason,
    messages: tuple[str, ...],
    match=None,
    live_state: LiveStateSnapshot | None = None,
    unresolved: tuple[str, ...] = (),
) -> LiveEdgeEvaluation:
    from app.opportunity_intel.live_edge.models import MatchResult

    remaining = None
    best_hint = _best_hint(market)
    if market.end_at is not None:
        remaining = int((market.end_at - now).total_seconds())
    signal_type = signal_type_for(
        identity, market, now_remaining_seconds=remaining, best_price_hint=best_hint
    )
    return _base_eval(
        market,
        identity,
        now=now,
        signal_type=signal_type,
        match=match
        or MatchResult(MatchStatus.NO_MATCH, None, messages),
        probability=None,
        reasons=messages,
        risks=(),
        skip_reason=reason,
        live_state=live_state,
        unresolved=unresolved,
    )


def _best_hint(market: DiscoveredMarket) -> Decimal | None:
    hints = [hint for hint in market.price_hints if hint is not None]
    return max(hints) if hints else None


def _picked_state(
    snapshots: tuple[LiveStateSnapshot, ...], match, identity: MarketIdentity
) -> LiveStateSnapshot:
    state = next(
        (
            item
            for item in snapshots
            if item.provenance
            and item.provenance.event_identifier == match.event_identifier
        ),
        snapshots[0],
    )
    return align_snapshot_to_market(state, identity)


class UnsupportedEvaluator:
    def __init__(self, category: LiveEdgeCategory) -> None:
        self.category = category

    def evaluate(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        snapshots: tuple[LiveStateSnapshot, ...],
        *,
        now: datetime,
    ) -> LiveEdgeEvaluation:
        _ = snapshots
        return _insufficient(
            market,
            identity,
            now=now,
            reason=SkipReason.UNSUPPORTED_CATEGORY,
            messages=(
                "категорія без надійного evaluator або недостатньо перевірених даних",
            ),
        )


class EsportsEvaluator:
    def __init__(self, category: LiveEdgeCategory) -> None:
        self.category = category

    def evaluate(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        snapshots: tuple[LiveStateSnapshot, ...],
        *,
        now: datetime,
    ) -> LiveEdgeEvaluation:
        if not snapshots:
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.INSUFFICIENT_DATA,
                messages=("немає перевіреного live-стану для цієї події",),
            )
        if any(bool(item.extra.get("source_conflict")) for item in snapshots):
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.SOURCE_CONFLICT,
                messages=("конфлікт live-джерел — BUY заборонено",),
                live_state=snapshots[0],
            )
        match = match_event(market, identity, snapshots, now=now)
        if match.status == MatchStatus.PROBABLE_MATCH:
            state = _picked_state(snapshots, match, identity)
            return _live_candidate(
                market,
                identity,
                now=now,
                match=match,
                live_state=state,
                messages=(
                    "PROBABLE_MATCH — live event зіставлено, але не VERIFIED",
                    *match.reasons,
                ),
            )
        if match.status != MatchStatus.VERIFIED_MATCH:
            reason = (
                SkipReason.AMBIGUOUS_MATCH
                if match.status == MatchStatus.AMBIGUOUS
                else SkipReason.NO_MATCH
            )
            if identity.kind in {MarketKind.MAP_WINNER, MarketKind.GAME_WINNER}:
                if any("finished earlier map" in item for item in match.reasons):
                    reason = SkipReason.MAP_ALREADY_FINISHED
                elif any("live state is map/game" in item for item in match.reasons):
                    reason = SkipReason.WRONG_MAP
            return _insufficient(
                market,
                identity,
                now=now,
                reason=reason,
                messages=match.reasons,
                match=match,
            )
        state = _picked_state(snapshots, match, identity)
        remaining = (
            int((market.end_at - now).total_seconds()) if market.end_at is not None else None
        )
        risks = ("подія ще не завершена",)
        if identity.kind in {MarketKind.MAP_WINNER, MarketKind.GAME_WINNER}:
            mapped = map_win_probability(state)
            if mapped is None:
                return _live_candidate(
                    market,
                    identity,
                    now=now,
                    match=match,
                    live_state=state,
                    messages=("недостатньо метрик поточної карти для HIGH probability",),
                )
            probability, tier = mapped
            if tier == EvidenceTier.LOW:
                return _live_candidate(
                    market,
                    identity,
                    now=now,
                    match=match,
                    live_state=state,
                    messages=("evidence tier LOW — немає BUY, зберігаємо live candidate",),
                    probability=probability,
                    evidence_tier=tier,
                )
            reasons = [
                "велика перевага саме в цій карті",
                "перевірене live-джерело",
            ]
            if state.kills is not None:
                reasons.append(f"рахунок убивств {state.kills[0]}–{state.kills[1]}")
            if state.gold_difference is not None:
                reasons.append(f"перевага по золоту {state.gold_difference:+d}")
            if state.round_score is not None:
                reasons.append(f"рахунок раундів {state.round_score[0]}–{state.round_score[1]}")
            return _base_eval(
                market,
                identity,
                now=now,
                signal_type=LiveEdgeSignalType.LIVE_MAP_EDGE,
                match=match,
                probability=probability,
                reasons=tuple(reasons),
                risks=risks,
                skip_reason=None,
                live_state=state,
                evidence_tier=tier,
            )
        if state.maps_won is None or state.series_format is None:
            return _live_candidate(
                market,
                identity,
                now=now,
                match=match,
                live_state=state,
                messages=("немає рахунку серії або формату BO — live event зіставлено",),
            )
        try:
            best_of = int(str(state.series_format).upper().replace("BO", ""))
        except ValueError:
            return _live_candidate(
                market,
                identity,
                now=now,
                match=match,
                live_state=state,
                messages=("невідомий формат серії — live event зіставлено",),
            )
        current = map_win_probability(state)
        current_range = current[0] if current is not None else None
        series = series_win_probability(
            state.maps_won, best_of=best_of, current_map_p=current_range
        )
        if series is None:
            return _live_candidate(
                market,
                identity,
                now=now,
                match=match,
                live_state=state,
                messages=("недостатньо даних для консервативної ймовірності серії",),
            )
        probability, tier = series
        if current is not None:
            order = {EvidenceTier.LOW: 0, EvidenceTier.MEDIUM: 1, EvidenceTier.HIGH: 2}
            if order[current[1]] > order[tier]:
                tier = current[1]
        if tier == EvidenceTier.LOW:
            return _live_candidate(
                market,
                identity,
                now=now,
                match=match,
                live_state=state,
                messages=("evidence tier LOW — немає BUY, зберігаємо live candidate",),
                probability=probability,
                evidence_tier=tier,
            )
        reasons = [
            f"рахунок по картах {state.maps_won[0]}–{state.maps_won[1]}",
            "перевірене live-джерело",
            "ймовірність серії з remaining-map combinatorics",
        ]
        if current_range is not None:
            reasons.append("враховано стан поточної карти")
        return _base_eval(
            market,
            identity,
            now=now,
            signal_type=LiveEdgeSignalType.LIVE_MATCH_EDGE,
            match=match,
            probability=probability,
            reasons=tuple(reasons),
            risks=risks,
            skip_reason=None,
            live_state=state,
            evidence_tier=tier,
        )


class SportsEvaluator:
    def __init__(self, category: LiveEdgeCategory) -> None:
        self.category = category

    def evaluate(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        snapshots: tuple[LiveStateSnapshot, ...],
        *,
        now: datetime,
    ) -> LiveEdgeEvaluation:
        if not snapshots:
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.INSUFFICIENT_DATA,
                messages=("немає перевіреного live-стану",),
            )
        match = match_event(market, identity, snapshots, now=now)
        if match.status == MatchStatus.PROBABLE_MATCH:
            state = _picked_state(snapshots, match, identity)
            return _live_candidate(
                market,
                identity,
                now=now,
                match=match,
                live_state=state,
                messages=(
                    "PROBABLE_MATCH — live event зіставлено, але не VERIFIED",
                    *match.reasons,
                ),
            )
        if match.status != MatchStatus.VERIFIED_MATCH:
            return _insufficient(
                market,
                identity,
                now=now,
                reason=(
                    SkipReason.AMBIGUOUS_MATCH
                    if match.status == MatchStatus.AMBIGUOUS
                    else SkipReason.NO_MATCH
                ),
                messages=match.reasons,
                match=match,
            )
        state = _picked_state(snapshots, match, identity)
        sport = {
            LiveEdgeCategory.TENNIS: "tennis",
            LiveEdgeCategory.BASKETBALL: "basketball",
            LiveEdgeCategory.BASEBALL: "baseball",
            LiveEdgeCategory.SOCCER: "soccer",
        }.get(self.category, "")
        mapped = traditional_sport_probability(sport, state)
        if mapped is None:
            return _live_candidate(
                market,
                identity,
                now=now,
                match=match,
                live_state=state,
                messages=("live-контекст недостатній для HIGH probability",),
            )
        probability, tier = mapped
        if tier == EvidenceTier.LOW:
            return _live_candidate(
                market,
                identity,
                now=now,
                match=match,
                live_state=state,
                messages=("evidence tier LOW — немає BUY, зберігаємо live candidate",),
                probability=probability,
                evidence_tier=tier,
            )
        remaining = (
            int((market.end_at - now).total_seconds()) if market.end_at is not None else None
        )
        return _base_eval(
            market,
            identity,
            now=now,
            signal_type=signal_type_for(
                identity, market, now_remaining_seconds=remaining, best_price_hint=_best_hint(market)
            ),
            match=match,
            probability=probability,
            reasons=("перевірений live-стан", "консервативна оцінка за доступними метриками"),
            risks=("подія ще не завершена",),
            skip_reason=None,
            live_state=state,
            evidence_tier=tier,
        )



class WeatherEvaluator:
    category = LiveEdgeCategory.WEATHER

    def evaluate(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        snapshots: tuple[LiveStateSnapshot, ...],
        *,
        now: datetime,
    ) -> LiveEdgeEvaluation:
        if not snapshots:
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.INSUFFICIENT_DATA,
                messages=("немає перевіреного weather evidence",),
            )
        state = snapshots[0]
        confirmed = state.extra.get("outcome_confirmed")
        unresolved = tuple(state.extra.get("unresolved_conditions") or ())
        remaining = (
            int((market.end_at - now).total_seconds()) if market.end_at is not None else None
        )
        near = is_near_resolution_candidate(
            remaining_seconds=remaining, price_hint=_best_hint(market)
        )
        if state.extra.get("resolution_ambiguous"):
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.AMBIGUOUS_RESOLUTION,
                messages=("умови resolution неоднозначні",),
                live_state=state,
            )
        if confirmed is True and unresolved:
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.UNRESOLVED_CONDITION,
                messages=unresolved,
                live_state=state,
                unresolved=unresolved,
            )
        if confirmed is not True:
            station = str(state.extra.get("station") or "").strip()
            if station and (near or market.live is True):
                from app.opportunity_intel.live_edge.models import MatchResult

                return _base_eval(
                    market,
                    identity,
                    now=now,
                    signal_type=(
                        LiveEdgeSignalType.NEAR_RESOLUTION_EDGE
                        if near
                        else LiveEdgeSignalType.LIVE_GENERAL_EDGE
                    ),
                    match=MatchResult(
                        MatchStatus.VERIFIED_MATCH,
                        state.provenance.event_identifier if state.provenance else "weather",
                        ("станція зіставлена; outcome ще не confirmed",),
                    ),
                    probability=None,
                    reasons=(
                        "weather station/METAR є",
                        "outcome не confirmed — WATCH, не BUY",
                    ),
                    risks=unresolved or ("день ще не закритий official source",),
                    skip_reason=None,
                    live_state=state,
                    unresolved=(),
                    near_resolution=near,
                    evidence_tier=EvidenceTier.LOW,
                )
            return _insufficient(
                market,
                identity,
                now=now,
                reason=(
                    SkipReason.UNRESOLVED_CONDITION
                    if unresolved
                    else SkipReason.NEAR_RESOLUTION_NO_EVIDENCE
                ),
                messages=unresolved or ("зовнішнє підтвердження результату відсутнє",),
                live_state=state,
                unresolved=unresolved,
            )
        low = Decimal(str(state.extra.get("conservative_low", "0")))
        high = Decimal(str(state.extra.get("conservative_high", "0")))
        if low <= _ZERO:
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.INSUFFICIENT_DATA,
                messages=("weather evidence не дає ймовірності",),
                live_state=state,
            )
        from app.opportunity_intel.live_edge.models import MatchResult

        remaining = (
            int((market.end_at - now).total_seconds()) if market.end_at is not None else None
        )
        near = is_near_resolution_candidate(
            remaining_seconds=remaining, price_hint=_best_hint(market)
        )
        return _base_eval(
            market,
            identity,
            now=now,
            signal_type=(
                LiveEdgeSignalType.NEAR_RESOLUTION_EDGE
                if near
                else LiveEdgeSignalType.LIVE_GENERAL_EDGE
            ),
            match=MatchResult(
                MatchStatus.VERIFIED_MATCH,
                state.provenance.event_identifier if state.provenance else "weather",
                ("станція та дата зіставлені з правилами resolution",),
            ),
            probability=ProbabilityRange(low, high),
            reasons=("перевірений weather state", "primary resolution source не змінено"),
            risks=("офіційне закриття дня ще може уточнити значення",),
            skip_reason=None,
            live_state=state,
            resolution_note=str(state.extra.get("resolution_note") or "") or None,
            near_resolution=near,
            evidence_tier=EvidenceTier.HIGH,
        )


class NearResolutionOverlay:
    """Applies near-resolution gates; >=85% market probability is discovery only."""

    def apply(self, evaluation: LiveEdgeEvaluation, market: DiscoveredMarket) -> LiveEdgeEvaluation:
        remaining = evaluation.estimated_end_in_seconds
        hint = _best_hint(market)
        if not is_near_resolution_candidate(remaining_seconds=remaining, price_hint=hint):
            return evaluation
        if evaluation.probability is None:
            return evaluation
        if evaluation.skip_reason is not None:
            return evaluation
        return replace(
            evaluation,
            signal_type=LiveEdgeSignalType.NEAR_RESOLUTION_EDGE,
            reasons=evaluation.reasons + ("ринок близький до resolution",),
            near_resolution=True,
        )


class CryptoEvaluator:
    category = LiveEdgeCategory.CRYPTO

    def evaluate(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        snapshots: tuple[LiveStateSnapshot, ...],
        *,
        now: datetime,
    ) -> LiveEdgeEvaluation:
        if not snapshots:
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.INSUFFICIENT_DATA,
                messages=("немає перевіреної спотової ціни",),
            )
        state = snapshots[0]
        if state.extra.get("outcome_confirmed") is not True:
            return _insufficient(
                market,
                identity,
                now=now,
                reason=SkipReason.NEAR_RESOLUTION_NO_EVIDENCE,
                messages=(
                    "спотова ціна є, але немає calibrated model і outcome ще не locked",
                ),
                live_state=state,
            )
        return _insufficient(
            market,
            identity,
            now=now,
            reason=SkipReason.INSUFFICIENT_DATA,
            messages=("crypto outcome не підтверджено official resolution source",),
            live_state=state,
        )


class OfficialRulesEvaluator:
    """Non-sports: parse resolution rules, never invent probability."""

    def __init__(self, category: LiveEdgeCategory) -> None:
        self.category = category

    def evaluate(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        snapshots: tuple[LiveStateSnapshot, ...],
        *,
        now: datetime,
    ) -> LiveEdgeEvaluation:
        _ = snapshots
        text = " ".join(
            part for part in (market.question, market.description, market.event_title) if part
        )
        unresolved = []
        if not market.description:
            unresolved.append("немає тексту resolution rules")
        lower = text.lower()
        if any(word in lower for word in ("or later", "unclear", "subject to", "may be delayed")):
            unresolved.append("умови resolution неоднозначні")
        if snapshots:
            state = snapshots[0]
            if state.extra.get("outcome_confirmed") is True:
                low = Decimal(str(state.extra.get("conservative_low", "0")))
                high = Decimal(str(state.extra.get("conservative_high", "0")))
                if low > _ZERO:
                    from app.opportunity_intel.live_edge.models import MatchResult

                    remaining = (
                        int((market.end_at - now).total_seconds())
                        if market.end_at is not None
                        else None
                    )
                    return _base_eval(
                        market,
                        identity,
                        now=now,
                        signal_type=LiveEdgeSignalType.NEAR_RESOLUTION_EDGE,
                        match=MatchResult(
                            MatchStatus.VERIFIED_MATCH,
                            state.provenance.event_identifier if state.provenance else "official",
                            ("офіційне evidence підтверджує outcome",),
                        ),
                        probability=ProbabilityRange(low, high),
                        reasons=("офіційне/authoritative evidence",),
                        risks=tuple(unresolved) or ("resolution ще може бути уточнене",),
                        skip_reason=None,
                        live_state=state,
                        unresolved=(),
                        near_resolution=True,
                        evidence_tier=EvidenceTier.HIGH,
                    )
        return _insufficient(
            market,
            identity,
            now=now,
            reason=SkipReason.UNSUPPORTED_CATEGORY,
            messages=(
                "ринок discovered; немає official evidence для probability",
                *tuple(unresolved),
            ),
            unresolved=tuple(unresolved),
        )



EVALUATORS: dict[LiveEdgeCategory, CategoryEvaluator] = {
    LiveEdgeCategory.ESPORTS_LOL: EsportsEvaluator(LiveEdgeCategory.ESPORTS_LOL),
    LiveEdgeCategory.ESPORTS_CS2: EsportsEvaluator(LiveEdgeCategory.ESPORTS_CS2),
    LiveEdgeCategory.ESPORTS_DOTA: EsportsEvaluator(LiveEdgeCategory.ESPORTS_DOTA),
    LiveEdgeCategory.TENNIS: SportsEvaluator(LiveEdgeCategory.TENNIS),
    LiveEdgeCategory.BASKETBALL: SportsEvaluator(LiveEdgeCategory.BASKETBALL),
    LiveEdgeCategory.BASEBALL: SportsEvaluator(LiveEdgeCategory.BASEBALL),
    LiveEdgeCategory.SOCCER: SportsEvaluator(LiveEdgeCategory.SOCCER),
    LiveEdgeCategory.WEATHER: WeatherEvaluator(),
    LiveEdgeCategory.CRYPTO: CryptoEvaluator(),
    LiveEdgeCategory.POLITICS: OfficialRulesEvaluator(LiveEdgeCategory.POLITICS),
    LiveEdgeCategory.GEOPOLITICS: OfficialRulesEvaluator(LiveEdgeCategory.GEOPOLITICS),
    LiveEdgeCategory.ECONOMICS: OfficialRulesEvaluator(LiveEdgeCategory.ECONOMICS),
    LiveEdgeCategory.TECHNOLOGY: OfficialRulesEvaluator(LiveEdgeCategory.TECHNOLOGY),
    LiveEdgeCategory.GENERAL: OfficialRulesEvaluator(LiveEdgeCategory.GENERAL),
    LiveEdgeCategory.UNSUPPORTED: UnsupportedEvaluator(LiveEdgeCategory.UNSUPPORTED),
}

SUPPORTED_EVALUATOR_CATEGORIES: frozenset[LiveEdgeCategory] = frozenset(
    {
        LiveEdgeCategory.ESPORTS_LOL,
        LiveEdgeCategory.ESPORTS_CS2,
        LiveEdgeCategory.ESPORTS_DOTA,
        LiveEdgeCategory.TENNIS,
        LiveEdgeCategory.BASKETBALL,
        LiveEdgeCategory.BASEBALL,
        LiveEdgeCategory.SOCCER,
        LiveEdgeCategory.WEATHER,
    }
)


def evaluate_market(
    market: DiscoveredMarket,
    identity: MarketIdentity,
    provider: LiveStateProvider | None = None,
    *,
    now: datetime | None = None,
) -> LiveEdgeEvaluation:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source = provider or NullLiveStateProvider()
    snapshots = source.snapshots_for(market, identity, now=clock)
    if isinstance(snapshots, LiveStateSnapshot):
        snapshots = (snapshots,)
    snapshots = tuple(snapshots or ())
    evaluator = EVALUATORS.get(identity.category) or EVALUATORS[LiveEdgeCategory.UNSUPPORTED]
    if identity.category in {
        LiveEdgeCategory.GENERAL,
        LiveEdgeCategory.UNSUPPORTED,
    } and snapshots:
        sport = snapshots[0].sport.lower()
        routed = _evaluator_for_sport(sport)
        if routed is not None:
            evaluator = routed
    result = evaluator.evaluate(market, identity, snapshots, now=clock)
    return NearResolutionOverlay().apply(result, market)


def _evaluator_for_sport(sport: str) -> CategoryEvaluator | None:
    if "league of legends" in sport or sport == "lol":
        return EVALUATORS[LiveEdgeCategory.ESPORTS_LOL]
    if "counter-strike" in sport or sport in {"cs2", "csgo"}:
        return EVALUATORS[LiveEdgeCategory.ESPORTS_CS2]
    if "dota" in sport:
        return EVALUATORS[LiveEdgeCategory.ESPORTS_DOTA]
    if "tennis" in sport:
        return EVALUATORS[LiveEdgeCategory.TENNIS]
    if "basketball" in sport:
        return EVALUATORS[LiveEdgeCategory.BASKETBALL]
    if "baseball" in sport:
        return EVALUATORS[LiveEdgeCategory.BASEBALL]
    if "soccer" in sport or "football" in sport:
        return EVALUATORS[LiveEdgeCategory.SOCCER]
    if "weather" in sport:
        return EVALUATORS[LiveEdgeCategory.WEATHER]
    return None
