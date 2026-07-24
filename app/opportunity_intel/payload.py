"""Immutable, renderer-ready delivery card payload (no Telegram markup)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.opportunity_intel.models import (
    MovementDirection,
    ObservationMode,
    OpportunityReasonCode,
    SignalFamily,
    _safe_card,
    _utc,
    decimal_text,
)
from app.polymarket.links import build_validated_market_link

_PAYLOAD_KEYS = (
    "opportunity_uid",
    "signal_at",
    "wave_at",
    "signal_family",
    "question",
    "outcome",
    "event_slug",
    "market_link",
    "mode",
    "canonical_score",
    "reason_codes",
    "direction",
    "move_5m",
    "move_15m",
    "move_1h",
    "best_bid",
    "best_ask",
    "midpoint",
    "absolute_spread",
    "relative_spread",
    "allocation_usd",
    "execution_vwap",
    "slippage",
    "executable_ask_depth_usd",
    "volume_24h_usd",
    "liquidity_usd",
    "end_at",
    "source_observed_at",
)


@dataclass(frozen=True)
class DeliveryCardPayload:
    opportunity_uid: str
    signal_at: datetime
    wave_at: datetime
    signal_family: SignalFamily
    question: str
    outcome: str
    event_slug: str | None
    market_link: str | None
    mode: ObservationMode
    canonical_score: Decimal
    reason_codes: tuple[OpportunityReasonCode, ...]
    direction: MovementDirection
    move_5m: Decimal
    move_15m: Decimal
    move_1h: Decimal
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    absolute_spread: Decimal
    relative_spread: Decimal
    allocation_usd: Decimal
    execution_vwap: Decimal
    slippage: Decimal
    executable_ask_depth_usd: Decimal
    volume_24h_usd: Decimal
    liquidity_usd: Decimal
    end_at: datetime
    source_observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_at", _utc(self.signal_at, "signal_at"))
        object.__setattr__(self, "wave_at", _utc(self.wave_at, "wave_at"))
        object.__setattr__(self, "end_at", _utc(self.end_at, "end_at"))
        object.__setattr__(
            self, "source_observed_at", _utc(self.source_observed_at, "source_observed_at")
        )
        if not self.reason_codes:
            raise ValueError("reason_codes must not be empty")
        if self.market_link is not None and self.event_slug is None:
            raise ValueError("market_link requires a validated event_slug")
        if self.event_slug is not None:
            link = build_validated_market_link(self.event_slug)
            if link is None:
                raise ValueError("event_slug is unsafe")
            if self.market_link is not None and self.market_link != link:
                raise ValueError("market_link does not match validated slug")

    def to_payload_text(self) -> str:
        text = json.dumps(self.to_sanitized_dict(), sort_keys=True, separators=(",", ":"))
        return _safe_card(text)

    def to_sanitized_dict(self) -> dict[str, Any]:
        slug = self.event_slug
        link = None if slug is None else build_validated_market_link(slug)
        return {
            "opportunity_uid": self.opportunity_uid,
            "signal_at": _iso(self.signal_at),
            "wave_at": _iso(self.wave_at),
            "signal_family": self.signal_family.value,
            "question": self.question,
            "outcome": self.outcome,
            "event_slug": slug if link is not None else None,
            "market_link": link,
            "mode": self.mode.value,
            "canonical_score": decimal_text(self.canonical_score),
            "reason_codes": [code.value for code in self.reason_codes],
            "direction": self.direction.value,
            "move_5m": decimal_text(self.move_5m),
            "move_15m": decimal_text(self.move_15m),
            "move_1h": decimal_text(self.move_1h),
            "best_bid": decimal_text(self.best_bid),
            "best_ask": decimal_text(self.best_ask),
            "midpoint": decimal_text(self.midpoint),
            "absolute_spread": decimal_text(self.absolute_spread),
            "relative_spread": decimal_text(self.relative_spread),
            "allocation_usd": decimal_text(self.allocation_usd),
            "execution_vwap": decimal_text(self.execution_vwap),
            "slippage": decimal_text(self.slippage),
            "executable_ask_depth_usd": decimal_text(self.executable_ask_depth_usd),
            "volume_24h_usd": decimal_text(self.volume_24h_usd),
            "liquidity_usd": decimal_text(self.liquidity_usd),
            "end_at": _iso(self.end_at),
            "source_observed_at": _iso(self.source_observed_at),
        }

    @classmethod
    def from_payload_text(cls, text: str) -> DeliveryCardPayload:
        _safe_card(text)
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("payload must be a JSON object")
        unexpected = sorted(set(raw) - set(_PAYLOAD_KEYS))
        if unexpected:
            raise ValueError(f"payload contains non-allowlisted keys: {unexpected}")
        missing = [key for key in _PAYLOAD_KEYS if key not in raw]
        if missing:
            raise ValueError(f"payload missing keys: {missing}")
        codes = tuple(OpportunityReasonCode(code) for code in raw["reason_codes"])
        return cls(
            opportunity_uid=str(raw["opportunity_uid"]),
            signal_at=datetime.fromisoformat(raw["signal_at"]),
            wave_at=datetime.fromisoformat(raw["wave_at"]),
            signal_family=SignalFamily(raw["signal_family"]),
            question=str(raw["question"]),
            outcome=str(raw["outcome"]),
            event_slug=None if raw["event_slug"] is None else str(raw["event_slug"]),
            market_link=None if raw["market_link"] is None else str(raw["market_link"]),
            mode=ObservationMode(raw["mode"]),
            canonical_score=Decimal(raw["canonical_score"]),
            reason_codes=codes,
            direction=MovementDirection(raw["direction"]),
            move_5m=Decimal(raw["move_5m"]),
            move_15m=Decimal(raw["move_15m"]),
            move_1h=Decimal(raw["move_1h"]),
            best_bid=Decimal(raw["best_bid"]),
            best_ask=Decimal(raw["best_ask"]),
            midpoint=Decimal(raw["midpoint"]),
            absolute_spread=Decimal(raw["absolute_spread"]),
            relative_spread=Decimal(raw["relative_spread"]),
            allocation_usd=Decimal(raw["allocation_usd"]),
            execution_vwap=Decimal(raw["execution_vwap"]),
            slippage=Decimal(raw["slippage"]),
            executable_ask_depth_usd=Decimal(raw["executable_ask_depth_usd"]),
            volume_24h_usd=Decimal(raw["volume_24h_usd"]),
            liquidity_usd=Decimal(raw["liquidity_usd"]),
            end_at=datetime.fromisoformat(raw["end_at"]),
            source_observed_at=datetime.fromisoformat(raw["source_observed_at"]),
        )


def build_delivery_card_payload(
    *,
    opportunity_uid: str,
    signal_at: datetime,
    wave_at: datetime,
    signal_family: SignalFamily,
    question: str,
    outcome: str,
    event_slug: str,
    mode: ObservationMode,
    canonical_score: Decimal,
    reason_codes: tuple[OpportunityReasonCode, ...],
    direction: MovementDirection,
    move_5m: Decimal,
    move_15m: Decimal,
    move_1h: Decimal,
    best_bid: Decimal,
    best_ask: Decimal,
    midpoint: Decimal,
    absolute_spread: Decimal,
    relative_spread: Decimal,
    allocation_usd: Decimal,
    execution_vwap: Decimal,
    slippage: Decimal,
    executable_ask_depth_usd: Decimal,
    volume_24h_usd: Decimal,
    liquidity_usd: Decimal,
    end_at: datetime,
    source_observed_at: datetime,
) -> DeliveryCardPayload:
    link = build_validated_market_link(event_slug)
    safe_slug = event_slug if link is not None else None
    return DeliveryCardPayload(
        opportunity_uid=opportunity_uid,
        signal_at=signal_at,
        wave_at=wave_at,
        signal_family=signal_family,
        question=question,
        outcome=outcome,
        event_slug=safe_slug,
        market_link=link,
        mode=mode,
        canonical_score=canonical_score,
        reason_codes=reason_codes,
        direction=direction,
        move_5m=move_5m,
        move_15m=move_15m,
        move_1h=move_1h,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        absolute_spread=absolute_spread,
        relative_spread=relative_spread,
        allocation_usd=allocation_usd,
        execution_vwap=execution_vwap,
        slippage=slippage,
        executable_ask_depth_usd=executable_ask_depth_usd,
        volume_24h_usd=volume_24h_usd,
        liquidity_usd=liquidity_usd,
        end_at=end_at,
        source_observed_at=source_observed_at,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
