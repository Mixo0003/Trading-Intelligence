"""Deterministic, read-only Polymarket trade-candidate decision policy.

This module never places orders.  It converts verified public market facts into
paper ENTER / WAIT / EXIT / SKIP states and keeps all arithmetic Decimal-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Iterable
from urllib.parse import urlparse

from app.copy_intel.order_book import ValidatedOrderBook
from app.opportunity_intel.history import HistoryDirection, MovementResult
from app.opportunity_intel.models import ExecutionMetrics, ObservationMode

EVIDENCE_MAX_AGE_SECONDS = 6 * 60 * 60
LIVE_STATE_MAX_AGE_SECONDS = 90
DEADLINE_DECAY_MAX_SECONDS = 30 * 24 * 60 * 60
MINIMUM_PROJECTED_NET_RETURN = Decimal("0.02")
MINIMUM_TARGET_MOVE = Decimal("0.03")
MAXIMUM_TARGET_MOVE = Decimal("0.10")
MINIMUM_TARGET_DISTANCE = Decimal("0.05")
MAXIMUM_TARGET_DISTANCE = Decimal("0.15")

_CENT = Decimal("0.0001")
_MONEY = Decimal("0.01")
_SPORT_CATEGORIES = frozenset({"sports", "esports", "e-sports", "games"})
_DEADLINE_WORDS = (" by ", " before ", " until ", " through ", " до ")


class TradeAction(StrEnum):
    ENTER = "ENTER"
    WAIT = "WAIT"
    EXIT = "EXIT"
    SKIP = "SKIP"


class TradeStage(StrEnum):
    PRE_MOVE_SETUP = "PRE_MOVE_SETUP"
    ENTRY_TRIGGER = "ENTRY_TRIGGER"
    MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
    EXECUTED_IMPULSE = "EXECUTED_IMPULSE"
    EXIT_TAKE_PROFIT = "EXIT / TAKE_PROFIT"
    WAIT = "WAIT"
    SKIP = "SKIP"


class SourceTier(StrEnum):
    OFFICIAL = "OFFICIAL"
    TRUSTED = "TRUSTED"
    SOCIAL = "SOCIAL"


class EvidenceDirection(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONFLICTS = "CONFLICTS"


class CatalystKind(StrEnum):
    OFFICIAL_LIVE_ADVANTAGE = "OFFICIAL_LIVE_ADVANTAGE"
    VERIFIED_NEWS = "VERIFIED_NEWS"
    DEADLINE_DECAY = "DEADLINE_DECAY"


@dataclass(frozen=True)
class ResolutionEvidence:
    rules_text: str
    source_url: str

    @property
    def verified(self) -> bool:
        parsed = urlparse(self.source_url)
        rules = self.rules_text.strip().casefold()
        source_defined_in_rules = any(
            phrase in rules
            for phrase in (
                "consensus of credible reporting",
                "official information from",
                "primary resolution source",
                "resolution source for this market",
            )
        )
        return (
            len(rules) >= 20
            and (
                (parsed.scheme in {"http", "https"} and bool(parsed.netloc))
                or source_defined_in_rules
            )
        )


@dataclass(frozen=True)
class LiveMatchState:
    event_slug: str
    observed_at: datetime
    live: bool
    ended: bool
    score: str | None
    period: str | None
    elapsed: str | None
    outcome_leading: bool | None
    source_name: str = "Polymarket official sports feed"

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("live observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(timezone.utc))


@dataclass(frozen=True)
class MarketEvidence:
    source_id: str
    source_name: str
    source_url: str
    tier: SourceTier
    direction: EvidenceDirection
    published_at: datetime
    market_id: str
    event_slug: str
    outcome: str
    entity_match: bool
    date_match: bool
    outcome_match: bool

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("evidence published_at must be timezone-aware")
        object.__setattr__(self, "published_at", self.published_at.astimezone(timezone.utc))


@dataclass(frozen=True)
class EvidenceVerdict:
    confirmed: bool
    conflict: bool
    accepted_sources: tuple[str, ...]
    social_warnings: tuple[str, ...]


@dataclass(frozen=True)
class FeeSchedule:
    enabled: bool
    rate: Decimal
    exponent: int = 1
    taker_only: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite():
            raise TypeError("fee rate must be a finite Decimal")
        if self.rate < 0 or self.rate > 1:
            raise ValueError("fee rate must be between zero and one")
        if self.exponent != 1:
            raise ValueError("only the documented exponent=1 fee curve is supported")


@dataclass(frozen=True)
class ExitCapacity:
    fully_executable: bool
    quantity: Decimal
    vwap: Decimal | None
    slippage: Decimal | None


@dataclass(frozen=True)
class ProfitProjection:
    entry_price: Decimal
    target_price: Decimal
    projected_exit_price: Decimal
    quantity: Decimal
    entry_fee_usd: Decimal
    exit_fee_usd: Decimal
    gross_profit_usd: Decimal
    net_profit_usd: Decimal
    net_return: Decimal


@dataclass(frozen=True)
class TradeCandidateDecision:
    action: TradeAction
    stage: TradeStage
    current_price: Decimal
    maximum_entry_price: Decimal
    target_price: Decimal
    invalidation: str
    execution_quality: str
    liquidity_quality: str
    entry_slippage: Decimal
    exit_slippage: Decimal | None
    projection: ProfitProjection | None
    catalysts: tuple[CatalystKind, ...]
    evidence_lines: tuple[str, ...]
    conclusion: str


def evaluate_market_evidence(
    evidence: Iterable[MarketEvidence],
    *,
    now: datetime,
    market_id: str,
    event_slug: str,
    outcome: str,
) -> EvidenceVerdict:
    """Require one official or two independent trusted exact-market sources."""
    clock = now.astimezone(timezone.utc)
    accepted: list[MarketEvidence] = []
    social: list[str] = []
    for item in evidence:
        age = (clock - item.published_at).total_seconds()
        exact = (
            item.market_id == market_id
            and item.event_slug == event_slug
            and item.outcome.casefold() == outcome.casefold()
            and item.entity_match
            and item.date_match
            and item.outcome_match
            and 0 <= age <= EVIDENCE_MAX_AGE_SECONDS
        )
        if not exact:
            continue
        if item.tier == SourceTier.SOCIAL:
            social.append(item.source_name)
            continue
        accepted.append(item)

    supporting = {item.source_id for item in accepted if item.direction == EvidenceDirection.SUPPORTS}
    conflicting = {item.source_id for item in accepted if item.direction == EvidenceDirection.CONFLICTS}
    conflict = bool(conflicting) or bool(supporting & conflicting)
    official = any(
        item.tier == SourceTier.OFFICIAL and item.direction == EvidenceDirection.SUPPORTS
        for item in accepted
    )
    trusted_count = len(
        {
            item.source_id
            for item in accepted
            if item.tier == SourceTier.TRUSTED
            and item.direction == EvidenceDirection.SUPPORTS
        }
    )
    return EvidenceVerdict(
        confirmed=(official or trusted_count >= 2) and not conflict,
        conflict=conflict,
        accepted_sources=tuple(dict.fromkeys(item.source_name for item in accepted)),
        social_warnings=tuple(dict.fromkeys(social)),
    )


def taker_fee_usd(*, shares: Decimal, price: Decimal, schedule: FeeSchedule) -> Decimal:
    """Official V2 fee curve: shares * rate * price * (1-price)."""
    if shares < 0 or not Decimal("0") <= price <= Decimal("1"):
        raise ValueError("invalid fee inputs")
    if not schedule.enabled or schedule.rate == 0:
        return Decimal("0")
    return (shares * schedule.rate * price * (Decimal("1") - price)).quantize(
        Decimal("0.00001"), rounding=ROUND_HALF_UP
    )


def sweep_sell_capacity(book: ValidatedOrderBook, quantity: Decimal) -> ExitCapacity:
    """Sweep bid shares for an exit-capacity and current-impact proof."""
    if quantity <= 0:
        return ExitCapacity(False, quantity, None, None)
    remaining = quantity
    proceeds = Decimal("0")
    filled = Decimal("0")
    for level in sorted(book.bids, key=lambda item: item.price, reverse=True):
        price = Decimal(str(level.price))
        available = Decimal(str(level.size))
        take = min(remaining, available)
        proceeds += take * price
        filled += take
        remaining -= take
        if remaining <= Decimal("0.00000001"):
            break
    if remaining > Decimal("0.00000001") or filled <= 0:
        return ExitCapacity(False, quantity, None, None)
    vwap = proceeds / filled
    best_bid = Decimal(str(book.best_bid))
    slippage = (best_bid - vwap) / best_bid if best_bid > 0 else Decimal("1")
    return ExitCapacity(True, quantity, vwap, max(Decimal("0"), slippage))


def build_trade_candidate(
    *,
    market_id: str,
    event_slug: str,
    question: str,
    outcome: str,
    category_ids: tuple[str, ...],
    mode: ObservationMode,
    end_at: datetime,
    now: datetime,
    resolution: ResolutionEvidence,
    live_state: LiveMatchState | None,
    evidence: Iterable[MarketEvidence],
    movement: MovementResult,
    book: ValidatedOrderBook,
    entry_execution: ExecutionMetrics,
    fee_schedule: FeeSchedule,
    maximum_slippage: Decimal,
    target_price_hint: Decimal | None = None,
) -> TradeCandidateDecision:
    """Build one conservative paper decision from fully validated public inputs."""
    clock = now.astimezone(timezone.utc)
    current = entry_execution.execution_vwap
    entry_slippage = entry_execution.slippage
    if (
        not resolution.verified
        or not entry_execution.fully_executable
        or current is None
        or entry_slippage is None
    ):
        return _skip(
            current=current or Decimal(str(book.best_ask)),
            entry_slippage=entry_slippage or Decimal("0"),
            reason="Правила резолюції або виконуваний вхід не підтверджені.",
        )

    verdict = evaluate_market_evidence(
        evidence,
        now=clock,
        market_id=market_id,
        event_slug=event_slug,
        outcome=outcome,
    )
    if verdict.conflict:
        return _wait(
            current=current,
            maximum_entry=current,
            target=current,
            entry_slippage=entry_slippage,
            stage=TradeStage.WAIT,
            reason="Надійні джерела суперечать одне одному — чекати узгодження.",
            evidence_lines=("джерела суперечать одне одному;",),
        )

    categories = frozenset(category_ids)
    sports_market = bool(categories & _SPORT_CATEGORIES)
    live_confirmed = False
    live_advantage = False
    if sports_market:
        if live_state is not None:
            live_age = (clock - live_state.observed_at).total_seconds()
            live_confirmed = (
                live_state.event_slug == event_slug
                and 0 <= live_age <= LIVE_STATE_MAX_AGE_SECONDS
                and live_state.live
                and not live_state.ended
                and live_state.score is not None
            )
            live_advantage = live_confirmed and live_state.outcome_leading is True
        if not live_confirmed:
            return _wait(
                current=current,
                maximum_entry=current,
                target=current,
                entry_slippage=entry_slippage,
                stage=TradeStage.WAIT,
                reason="Офіційний live-стан матчу не підтверджений.",
                evidence_lines=("live-стан відсутній, застарілий або неоднозначний;",),
            )

    catalysts: list[CatalystKind] = []
    if live_advantage:
        catalysts.append(CatalystKind.OFFICIAL_LIVE_ADVANTAGE)
    if verdict.confirmed:
        catalysts.append(CatalystKind.VERIFIED_NEWS)
    remaining = (end_at.astimezone(timezone.utc) - clock).total_seconds()
    deadline_decay = (
        outcome.strip().casefold() in {"no", "ні"}
        and any(token in f" {question.casefold()} " for token in _DEADLINE_WORDS)
        and 0 < remaining <= DEADLINE_DECAY_MAX_SECONDS
    )
    if deadline_decay:
        catalysts.append(CatalystKind.DEADLINE_DECAY)

    strongest = _strongest_move(movement)
    target_move = min(MAXIMUM_TARGET_MOVE, max(MINIMUM_TARGET_MOVE, strongest))
    target_anchor = movement.price_1h or movement.price_15m or current
    target = (
        derive_target_price(anchor_price=target_anchor, strongest_move=target_move)
        if target_price_hint is None
        else min(Decimal("0.99"), max(Decimal("0.0001"), target_price_hint)).quantize(_CENT)
    )
    maximum_entry = _maximum_entry(movement, current=current)

    projected_entry = min(current, maximum_entry)
    projected_quantity = entry_execution.allocation_usd / projected_entry
    exit_capacity = sweep_sell_capacity(book, projected_quantity)
    if not exit_capacity.fully_executable or exit_capacity.slippage is None:
        return _skip(
            current=current,
            entry_slippage=entry_slippage,
            reason="Стакан не дозволяє вийти потрібним обсягом.",
            maximum_entry=maximum_entry,
            target=target,
        )
    if entry_slippage > maximum_slippage or exit_capacity.slippage > maximum_slippage:
        return _skip(
            current=current,
            entry_slippage=entry_slippage,
            reason="Прослизання входу або виходу перевищує дозволений рівень.",
            maximum_entry=maximum_entry,
            target=target,
            exit_slippage=exit_capacity.slippage,
        )

    projection = _project_profit(
        allocation_usd=entry_execution.allocation_usd,
        entry_price=projected_entry,
        target_price=target,
        exit_slippage=exit_capacity.slippage,
        fee_schedule=fee_schedule,
    )
    if target <= projected_entry or projection.net_profit_usd <= 0:
        return _skip(
            current=current,
            entry_slippage=entry_slippage,
            reason="Ціль не дає додатного результату після комісій і прослизання.",
            maximum_entry=maximum_entry,
            target=target,
            exit_slippage=exit_capacity.slippage,
            projection=projection,
        )
    evidence_lines = _evidence_lines(
        movement=movement,
        live_confirmed=live_confirmed,
        live_advantage=live_advantage,
        verdict=verdict,
        deadline_decay=deadline_decay,
        current=current,
        maximum_entry=maximum_entry,
    )
    invalidation = _invalidation(catalysts)
    quality = _execution_quality(entry_slippage, exit_capacity.slippage)

    if Decimal(str(book.best_bid)) >= target:
        return TradeCandidateDecision(
            action=TradeAction.EXIT,
            stage=TradeStage.EXIT_TAKE_PROFIT,
            current_price=current,
            maximum_entry_price=maximum_entry,
            target_price=target,
            invalidation=invalidation,
            execution_quality=quality,
            liquidity_quality="достатня",
            entry_slippage=entry_slippage,
            exit_slippage=exit_capacity.slippage,
            projection=projection,
            catalysts=tuple(catalysts),
            evidence_lines=evidence_lines,
            conclusion="Ціль досягнута: перевірити доступний bid та зафіксувати паперовий результат.",
        )

    if current > maximum_entry:
        return TradeCandidateDecision(
            action=TradeAction.WAIT,
            stage=TradeStage.EXECUTED_IMPULSE,
            current_price=current,
            maximum_entry_price=maximum_entry,
            target_price=target,
            invalidation=invalidation,
            execution_quality=quality,
            liquidity_quality="достатня",
            entry_slippage=entry_slippage,
            exit_slippage=exit_capacity.slippage,
            projection=projection,
            catalysts=tuple(catalysts),
            evidence_lines=evidence_lines,
            conclusion=(
                f"Не входити зараз. Чекати відкату до {maximum_entry:.4f} "
                "або нового підтвердженого каталізатора."
            ),
        )

    if not catalysts:
        return _wait(
            current=current,
            maximum_entry=maximum_entry,
            target=target,
            entry_slippage=entry_slippage,
            exit_slippage=exit_capacity.slippage,
            stage=TradeStage.WAIT,
            reason="Причина для подальшого руху не підтверджена.",
            evidence_lines=evidence_lines,
            projection=projection,
        )

    if projection.net_return < MINIMUM_PROJECTED_NET_RETURN:
        return _skip(
            current=current,
            entry_slippage=entry_slippage,
            reason="Очікуваний прибуток після комісій і прослизання недостатній.",
            maximum_entry=maximum_entry,
            target=target,
            exit_slippage=exit_capacity.slippage,
            projection=projection,
        )

    positive_momentum = movement.direction == HistoryDirection.UP and strongest > 0
    if not positive_momentum:
        return TradeCandidateDecision(
            action=TradeAction.WAIT,
            stage=TradeStage.PRE_MOVE_SETUP,
            current_price=current,
            maximum_entry_price=maximum_entry,
            target_price=target,
            invalidation=invalidation,
            execution_quality=quality,
            liquidity_quality="достатня",
            entry_slippage=entry_slippage,
            exit_slippage=exit_capacity.slippage,
            projection=projection,
            catalysts=tuple(catalysts),
            evidence_lines=evidence_lines,
            conclusion="Потенціал є, але ціновий тригер ще не підтверджений — чекати.",
        )

    stage = (
        TradeStage.MOMENTUM_CONTINUATION
        if strongest >= Decimal("0.02")
        else TradeStage.ENTRY_TRIGGER
    )
    return TradeCandidateDecision(
        action=TradeAction.ENTER,
        stage=stage,
        current_price=current,
        maximum_entry_price=maximum_entry,
        target_price=target,
        invalidation=invalidation,
        execution_quality=quality,
        liquidity_quality="достатня",
        entry_slippage=entry_slippage,
        exit_slippage=exit_capacity.slippage,
        projection=projection,
        catalysts=tuple(catalysts),
        evidence_lines=evidence_lines,
        conclusion=(
            "Умови паперового входу виконані; реальний ордер бот не створює."
        ),
    )


def _strongest_move(movement: MovementResult) -> Decimal:
    values = tuple(
        abs(value) / Decimal("100")
        for value in (
            movement.change_5m_pp,
            movement.change_15m_pp,
            movement.change_1h_pp,
        )
        if value is not None
    )
    return max(values, default=Decimal("0"))


def derive_target_price(*, anchor_price: Decimal, strongest_move: Decimal) -> Decimal:
    """Deterministic paper target anchored to the signal's original 1h price."""
    target_distance = min(
        MAXIMUM_TARGET_DISTANCE,
        max(MINIMUM_TARGET_DISTANCE, strongest_move * Decimal("2")),
    )
    return min(
        Decimal("0.99"),
        max(Decimal("0.0001"), anchor_price + target_distance),
    ).quantize(_CENT)


def _maximum_entry(movement: MovementResult, *, current: Decimal) -> Decimal:
    positive_impulse = movement.direction == HistoryDirection.UP and _strongest_move(movement) >= Decimal("0.02")
    anchors = tuple(
        value
        for value in (movement.price_5m, movement.price_15m)
        if value is not None and value < current
    )
    if positive_impulse and anchors:
        return max(anchors).quantize(_CENT)
    return current.quantize(_CENT)


def _project_profit(
    *,
    allocation_usd: Decimal,
    entry_price: Decimal,
    target_price: Decimal,
    exit_slippage: Decimal,
    fee_schedule: FeeSchedule,
) -> ProfitProjection:
    quantity = allocation_usd / entry_price
    projected_exit = max(
        Decimal("0.0001"), target_price * (Decimal("1") - exit_slippage)
    ).quantize(_CENT)
    entry_fee = taker_fee_usd(shares=quantity, price=entry_price, schedule=fee_schedule)
    exit_fee = taker_fee_usd(shares=quantity, price=projected_exit, schedule=fee_schedule)
    gross = quantity * (projected_exit - entry_price)
    net = gross - entry_fee - exit_fee
    cost = allocation_usd + entry_fee
    return ProfitProjection(
        entry_price=entry_price,
        target_price=target_price,
        projected_exit_price=projected_exit,
        quantity=quantity,
        entry_fee_usd=entry_fee,
        exit_fee_usd=exit_fee,
        gross_profit_usd=gross.quantize(_MONEY),
        net_profit_usd=net.quantize(_MONEY),
        net_return=(net / cost if cost > 0 else Decimal("-1")),
    )


def _evidence_lines(
    *,
    movement: MovementResult,
    live_confirmed: bool,
    live_advantage: bool,
    verdict: EvidenceVerdict,
    deadline_decay: bool,
    current: Decimal,
    maximum_entry: Decimal,
) -> tuple[str, ...]:
    lines: list[str] = []
    strongest = _strongest_move(movement)
    if strongest >= Decimal("0.02"):
        lines.append("імпульс уже відбувся;")
    if all(value is not None and value > 0 for value in (movement.change_5m_pp, movement.change_15m_pp, movement.change_1h_pp)):
        lines.append("рух підтверджений на 5м/15м/1г;")
    if live_confirmed:
        lines.append("офіційний live-стан матчу підтверджений;")
    if live_advantage:
        lines.append("результат має підтверджену live-перевагу;")
    if verdict.confirmed:
        lines.append("каталізатор підтверджений перевіреними джерелами;")
    elif not live_advantage and not deadline_decay:
        lines.append("новий каталізатор не підтверджений;")
    if deadline_decay:
        lines.append("наближення дедлайну підтримує результат No;")
    if current > maximum_entry:
        lines.append("поточна ціна вже вище оптимального входу.")
    else:
        lines.append("поточна ціна не перевищує допустимий вхід.")
    return tuple(lines)


def _invalidation(catalysts: Iterable[CatalystKind]) -> str:
    kinds = frozenset(catalysts)
    if CatalystKind.OFFICIAL_LIVE_ADVANTAGE in kinds:
        return "live-перевага результату зникає або офіційний статус суперечить прогнозу"
    if CatalystKind.DEADLINE_DECAY in kinds:
        return "подія відбувається до дедлайну або змінюються правила резолюції"
    if CatalystKind.VERIFIED_NEWS in kinds:
        return "офіційне джерело спростовує каталізатор або з'являється конфлікт джерел"
    return "причина майбутнього руху не підтверджується"


def _execution_quality(entry: Decimal, exit_value: Decimal) -> str:
    worst = max(entry, exit_value)
    if worst <= Decimal("0.01"):
        return "висока"
    if worst <= Decimal("0.02"):
        return "середня"
    return "низька"


def _wait(
    *,
    current: Decimal,
    maximum_entry: Decimal,
    target: Decimal,
    entry_slippage: Decimal,
    stage: TradeStage,
    reason: str,
    evidence_lines: tuple[str, ...],
    exit_slippage: Decimal | None = None,
    projection: ProfitProjection | None = None,
) -> TradeCandidateDecision:
    return TradeCandidateDecision(
        action=TradeAction.WAIT,
        stage=stage,
        current_price=current,
        maximum_entry_price=maximum_entry,
        target_price=target,
        invalidation="причина майбутнього руху не підтверджується",
        execution_quality="невизначена" if exit_slippage is None else _execution_quality(entry_slippage, exit_slippage),
        liquidity_quality="потребує підтвердження" if exit_slippage is None else "достатня",
        entry_slippage=entry_slippage,
        exit_slippage=exit_slippage,
        projection=projection,
        catalysts=(),
        evidence_lines=evidence_lines,
        conclusion=reason,
    )


def _skip(
    *,
    current: Decimal,
    entry_slippage: Decimal,
    reason: str,
    maximum_entry: Decimal | None = None,
    target: Decimal | None = None,
    exit_slippage: Decimal | None = None,
    projection: ProfitProjection | None = None,
) -> TradeCandidateDecision:
    return TradeCandidateDecision(
        action=TradeAction.SKIP,
        stage=TradeStage.SKIP,
        current_price=current,
        maximum_entry_price=maximum_entry or current,
        target_price=target or current,
        invalidation="ринок не проходить обов'язкові перевірки",
        execution_quality="недостатня",
        liquidity_quality="недостатня",
        entry_slippage=entry_slippage,
        exit_slippage=exit_slippage,
        projection=projection,
        catalysts=(),
        evidence_lines=(reason,),
        conclusion=reason,
    )


__all__ = [
    "CatalystKind",
    "EvidenceDirection",
    "EvidenceVerdict",
    "FeeSchedule",
    "LiveMatchState",
    "MarketEvidence",
    "ProfitProjection",
    "ResolutionEvidence",
    "SourceTier",
    "TradeAction",
    "TradeCandidateDecision",
    "TradeStage",
    "build_trade_candidate",
    "derive_target_price",
    "evaluate_market_evidence",
    "sweep_sell_capacity",
    "taker_fee_usd",
]
