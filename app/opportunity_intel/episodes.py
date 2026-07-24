"""Signal episode identity, material-change, and hysteresis rules."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database.opportunity_storage import MarketStateRecord
from app.opportunity_intel.history import MovementResult
from app.opportunity_intel.models import (
    MovementDirection,
    ObservationMode,
    OpportunityReasonCode,
    OpportunitySignal,
    SignalFamily,
    SignalStatus,
)
from app.opportunity_intel.observation import ModeThresholds, thresholds_for_mode
from app.opportunity_intel.versions import (
    NORMAL_MATERIAL_INTERVAL_SECONDS,
    NORMAL_MATERIAL_MOVE_PP,
    NORMAL_MATERIAL_SCORE_DELTA,
    OPPORTUNITY_SCORE_VERSION,
    SIGNAL_MAX_TTL_SECONDS,
    STRICT_MATERIAL_INTERVAL_SECONDS,
    STRICT_MATERIAL_MOVE_PP,
    STRICT_MATERIAL_SCORE_DELTA,
)


def build_opportunity_key(
    *,
    condition_id: str,
    asset_id: str,
    direction: MovementDirection,
    mode: ObservationMode,
    score_version: int = OPPORTUNITY_SCORE_VERSION,
) -> str:
    return (
        f"v{score_version}:{condition_id}:{asset_id}:"
        f"{SignalFamily.EXECUTABLE_MOMENTUM.value}:{direction.value}:{mode.value}"
    )


def build_opportunity_uid(
    *,
    opportunity_key: str,
    episode_sequence: int,
    material_change_sequence: int,
    source_observed_at: datetime,
) -> str:
    digest = hashlib.sha256(
        (
            f"{opportunity_key}|{episode_sequence}|{material_change_sequence}|"
            f"{source_observed_at.astimezone(timezone.utc).isoformat()}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"opp_{digest}"


def identity_fingerprint(
    *,
    condition_id: str,
    market_id: str,
    event_id: str,
    outcome: str,
) -> str:
    return f"{condition_id}|{market_id}|{event_id}|{outcome}"


def direction_from_movement(movement: MovementResult) -> MovementDirection | None:
    if movement.direction is None:
        return None
    value = movement.direction.value
    if value == "UP":
        return MovementDirection.UP
    if value == "DOWN":
        return MovementDirection.DOWN
    return None


def strongest_absolute_move_pp(movement: MovementResult) -> Decimal:
    values = (
        movement.absolute_change_5m_pp,
        movement.absolute_change_15m_pp,
        movement.absolute_change_1h_pp,
    )
    present = [value for value in values if value is not None]
    if not present:
        return Decimal("0")
    return max(present)


def observation_meets_floor(
    *,
    mode: ObservationMode,
    score: Decimal | None,
    movement: MovementResult,
) -> bool:
    thresholds = thresholds_for_mode(mode)
    if score is None or score < thresholds.minimum_score:
        return False
    if (
        movement.absolute_change_5m_pp is None
        or movement.absolute_change_15m_pp is None
        or movement.absolute_change_1h_pp is None
    ):
        return False
    return (
        movement.absolute_change_5m_pp >= thresholds.move_5m_pp
        or movement.absolute_change_15m_pp >= thresholds.move_15m_pp
        or movement.absolute_change_1h_pp >= thresholds.move_1h_pp
    )


def signal_expires_at(*, end_at: datetime, now: datetime) -> datetime:
    current = now.astimezone(timezone.utc)
    market_end = end_at.astimezone(timezone.utc)
    capped = current + timedelta(seconds=SIGNAL_MAX_TTL_SECONDS)
    expiry = market_end if market_end > current else current + timedelta(seconds=1)
    return min(expiry, capped)


@dataclass(frozen=True)
class EpisodeDecision:
    create_signal: OpportunitySignal | None = None
    expire_uids: tuple[str, ...] = ()
    material_updated: bool = False
    pending_live_direction: MovementDirection | None = None
    was_eligible_at_baseline: bool = False
    below_threshold_streak: int = 0
    last_signal_at: datetime | None = None


def decide_episode_actions(
    *,
    mode: ObservationMode,
    now: datetime,
    condition_id: str,
    asset_id: str,
    end_at: datetime,
    baseline_ready: bool,
    allow_new_episode: bool,
    prior: MarketStateRecord | None,
    eligible: bool,
    complete_below_floor: bool,
    score: Decimal | None,
    reason_codes: tuple[OpportunityReasonCode, ...],
    movement: MovementResult,
    source_observed_at: datetime,
    active_signals: tuple[OpportunitySignal, ...],
    market_closed: bool,
    next_episode_sequence: int,
) -> EpisodeDecision:
    """Deterministic post-baseline crossing, material change, and expiry.

    Post-baseline crossing rules:
    - Baseline cycles 1–2 never create signals.
    - If the market was already eligible when baseline became ready, suppress
      the initial episode until a two-observation below-floor streak clears
      ``was_eligible_at_baseline``.
    - A later eligible observation with suppress cleared creates episode 1.
    - Material re-alerts require interval + move/score/direction gates.
    """
    direction = direction_from_movement(movement)
    move_pp = strongest_absolute_move_pp(movement)
    was_eligible = bool(prior.was_eligible_at_baseline) if prior else False
    streak = int(prior.below_threshold_streak) if prior else 0
    pending = prior.pending_live_direction if prior else None
    last_signal_at = prior.last_signal_at if prior else None

    expire_uids: list[str] = []
    if market_closed:
        expire_uids.extend(signal.opportunity_uid for signal in active_signals)
        return EpisodeDecision(
            expire_uids=tuple(expire_uids),
            was_eligible_at_baseline=False,
            below_threshold_streak=0,
            pending_live_direction=None,
            last_signal_at=last_signal_at,
        )

    if complete_below_floor:
        streak = min(2, streak + 1)
        if streak >= 2:
            expire_uids.extend(signal.opportunity_uid for signal in active_signals)
            was_eligible = False
            streak = 0
        return EpisodeDecision(
            expire_uids=tuple(dict.fromkeys(expire_uids)),
            was_eligible_at_baseline=was_eligible,
            below_threshold_streak=streak,
            pending_live_direction=pending,
            last_signal_at=last_signal_at,
        )

    if not eligible or not baseline_ready:
        return EpisodeDecision(
            was_eligible_at_baseline=was_eligible,
            below_threshold_streak=streak if not eligible else 0,
            pending_live_direction=pending,
            last_signal_at=last_signal_at,
        )

    assert score is not None and direction is not None and reason_codes
    streak = 0
    key = build_opportunity_key(
        condition_id=condition_id,
        asset_id=asset_id,
        direction=direction,
        mode=mode,
    )
    active_for_key = tuple(
        signal for signal in active_signals if signal.opportunity_key == key
    )
    active = active_for_key[0] if active_for_key else None

    if active is None:
        if not allow_new_episode:
            return EpisodeDecision(
                was_eligible_at_baseline=True,
                below_threshold_streak=0,
                pending_live_direction=None,
                last_signal_at=last_signal_at,
            )
        if mode == ObservationMode.LIVE_STRICT:
            confirmed, pending = _strict_direction_gate(
                pending=pending,
                direction=direction,
            )
            if not confirmed:
                return EpisodeDecision(
                    was_eligible_at_baseline=False,
                    below_threshold_streak=0,
                    pending_live_direction=pending,
                    last_signal_at=last_signal_at,
                )
        signal = _new_signal(
            opportunity_key=key,
            asset_id=asset_id,
            episode_sequence=next_episode_sequence,
            material_change_sequence=0,
            direction=direction,
            mode=mode,
            score=score,
            reason_codes=reason_codes,
            source_observed_at=source_observed_at,
            end_at=end_at,
            now=now,
        )
        return EpisodeDecision(
            create_signal=signal,
            was_eligible_at_baseline=False,
            below_threshold_streak=0,
            pending_live_direction=None,
            last_signal_at=signal.signal_at,
        )

    if active.source_observed_at == source_observed_at:
        return EpisodeDecision(
            was_eligible_at_baseline=was_eligible,
            below_threshold_streak=0,
            pending_live_direction=None,
            last_signal_at=active.signal_at,
        )

    if mode == ObservationMode.LIVE_STRICT and active.direction != direction:
        confirmed, pending = _strict_direction_gate(
            pending=pending,
            direction=direction,
        )
        if not confirmed:
            return EpisodeDecision(
                was_eligible_at_baseline=was_eligible,
                below_threshold_streak=0,
                pending_live_direction=pending,
                last_signal_at=active.signal_at,
            )
        expire_uids.append(active.opportunity_uid)
        other_key = build_opportunity_key(
            condition_id=condition_id,
            asset_id=asset_id,
            direction=direction,
            mode=mode,
        )
        signal = _new_signal(
            opportunity_key=other_key,
            asset_id=asset_id,
            episode_sequence=next_episode_sequence,
            material_change_sequence=0,
            direction=direction,
            mode=mode,
            score=score,
            reason_codes=reason_codes,
            source_observed_at=source_observed_at,
            end_at=end_at,
            now=now,
        )
        return EpisodeDecision(
            create_signal=signal,
            expire_uids=tuple(expire_uids),
            material_updated=True,
            was_eligible_at_baseline=was_eligible,
            below_threshold_streak=0,
            pending_live_direction=None,
            last_signal_at=signal.signal_at,
        )

    if not _is_material_candidate(
        mode=mode,
        now=now,
        prior=prior,
        score=score,
        move_pp=move_pp,
        direction=direction,
        active=active,
        last_signal_at=active.signal_at,
    ):
        return EpisodeDecision(
            was_eligible_at_baseline=was_eligible,
            below_threshold_streak=0,
            pending_live_direction=None,
            last_signal_at=active.signal_at,
        )

    expire_uids.append(active.opportunity_uid)
    signal = _new_signal(
        opportunity_key=key,
        asset_id=asset_id,
        episode_sequence=active.episode_sequence,
        material_change_sequence=active.material_change_sequence + 1,
        direction=direction,
        mode=mode,
        score=score,
        reason_codes=reason_codes,
        source_observed_at=source_observed_at,
        end_at=end_at,
        now=now,
        first_seen_at=active.first_seen_at,
    )
    return EpisodeDecision(
        create_signal=signal,
        expire_uids=tuple(expire_uids),
        material_updated=True,
        was_eligible_at_baseline=was_eligible,
        below_threshold_streak=0,
        pending_live_direction=None,
        last_signal_at=signal.signal_at,
    )


def _strict_direction_gate(
    *,
    pending: MovementDirection | None,
    direction: MovementDirection,
) -> tuple[bool, MovementDirection | None]:
    if pending == direction:
        return True, None
    return False, direction


def _is_material_candidate(
    *,
    mode: ObservationMode,
    now: datetime,
    prior: MarketStateRecord | None,
    score: Decimal,
    move_pp: Decimal,
    direction: MovementDirection,
    active: OpportunitySignal | None,
    last_signal_at: datetime | None,
) -> bool:
    interval = (
        STRICT_MATERIAL_INTERVAL_SECONDS
        if mode == ObservationMode.LIVE_STRICT
        else NORMAL_MATERIAL_INTERVAL_SECONDS
    )
    move_need = (
        Decimal(STRICT_MATERIAL_MOVE_PP)
        if mode == ObservationMode.LIVE_STRICT
        else Decimal(NORMAL_MATERIAL_MOVE_PP)
    )
    score_need = (
        Decimal(STRICT_MATERIAL_SCORE_DELTA)
        if mode == ObservationMode.LIVE_STRICT
        else Decimal(NORMAL_MATERIAL_SCORE_DELTA)
    )
    if last_signal_at is None:
        return True
    elapsed = (now - last_signal_at.astimezone(timezone.utc)).total_seconds()
    if elapsed < interval:
        return False
    prior_move = prior.last_absolute_move_pp if prior else None
    prior_score = prior.last_score if prior else None
    if active is not None and active.direction != direction:
        return True
    if prior_move is not None and abs(move_pp - prior_move) >= move_need:
        return True
    if prior_score is not None and abs(score - prior_score) >= score_need:
        return True
    return False


def _new_signal(
    *,
    opportunity_key: str,
    asset_id: str,
    episode_sequence: int,
    material_change_sequence: int,
    direction: MovementDirection,
    mode: ObservationMode,
    score: Decimal,
    reason_codes: tuple[OpportunityReasonCode, ...],
    source_observed_at: datetime,
    end_at: datetime,
    now: datetime,
    first_seen_at: datetime | None = None,
) -> OpportunitySignal:
    uid = build_opportunity_uid(
        opportunity_key=opportunity_key,
        episode_sequence=episode_sequence,
        material_change_sequence=material_change_sequence,
        source_observed_at=source_observed_at,
    )
    first = first_seen_at or source_observed_at
    return OpportunitySignal(
        opportunity_uid=uid,
        opportunity_key=opportunity_key,
        asset_id=asset_id,
        episode_sequence=episode_sequence,
        material_change_sequence=material_change_sequence,
        family=SignalFamily.EXECUTABLE_MOMENTUM,
        direction=direction,
        mode=mode,
        score=score,
        reason_codes=reason_codes,
        signal_at=source_observed_at,
        first_seen_at=first,
        last_seen_at=source_observed_at,
        expires_at=signal_expires_at(end_at=end_at, now=now),
        source_observed_at=source_observed_at,
        status=SignalStatus.ACTIVE,
    )
