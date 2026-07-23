"""Stable copy-event identity helpers (no poll-time or observed_at fields)."""

from __future__ import annotations

from app.copy_intel.models import TradeEvent

_SIZE_DECIMALS = 6


def normalize_size(value: float | None) -> float:
    if value is None:
        return 0.0
    return round(float(value), _SIZE_DECIMALS)


def normalize_price(value: float | None) -> str:
    if value is None:
        return "na"
    return f"{round(float(value), _SIZE_DECIMALS):.{_SIZE_DECIMALS}f}"


def build_event_uid(event: TradeEvent, *, poll_seq: int = 0) -> str:
    """
    Strongest available stable identity.
    poll_seq is accepted for call-site compatibility but never used.
    """
    del poll_seq  # explicitly unstable — must not affect identity
    if event.event_uid:
        return event.event_uid

    condition = (event.condition_id or "").lower()
    outcome = (event.outcome or "").strip().lower()
    action = event.event_type

    if event.tx_hash:
        tx = event.tx_hash.lower()
        return f"tx:{tx}:{action}:{condition}:{outcome}"

    # Undocumented trade_id / id fields are never trusted for native identity.

    size_delta = normalize_size(event.size)
    prev = normalize_size(event.prev_size) if event.prev_size is not None else None
    new = normalize_size(event.new_size) if event.new_size is not None else None
    if prev is not None and new is not None:
        size_part = f"{prev:.{_SIZE_DECIMALS}f}->{new:.{_SIZE_DECIMALS}f}"
    else:
        size_part = f"{size_delta:.{_SIZE_DECIMALS}f}"
    ts_part = str(int(event.ts)) if event.ts is not None else "na"
    price_part = normalize_price(event.price)
    return (
        f"pd:{event.wallet}|{condition}|{outcome}|{action}|"
        f"{size_part}|{price_part}|{ts_part}"
    )
