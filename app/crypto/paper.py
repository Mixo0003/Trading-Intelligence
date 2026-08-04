"""Paper-only hooks reserved for later Crypto slices (no execution)."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.risk import assert_crypto_read_only


def assert_paper_only(settings: Settings) -> None:
    """Gate for future paper ledger writes — never places real orders."""
    assert_crypto_read_only(settings)
