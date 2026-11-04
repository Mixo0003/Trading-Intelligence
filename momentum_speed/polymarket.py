"""Read-only Polymarket access for «Моментум Спід».

Thin re-export layer over machinery that already enforces the
house rules — no new network code, and intentionally NO
order-placement code anywhere in this package:

  - window discovery + OFFICIAL Gamma resolution (slug
    {sym}-updown-5m-{window_start}, winner only once an outcome
    sits at ~1.0): paper_momentum_v18.polymarket;
  - full CLOB ask depth — the SAME book a live FAK order would
    execute against: paper_pair_accum.poly.book_asks.
"""

from __future__ import annotations

from paper_momentum_v18.polymarket import (  # noqa: F401
    get_window,
    official_winner,
    window_start_ts,
    winner_from_prices,
)
from paper_pair_accum.poly import book_asks  # noqa: F401

__all__ = [
    "get_window",
    "official_winner",
    "window_start_ts",
    "winner_from_prices",
    "book_asks",
]
