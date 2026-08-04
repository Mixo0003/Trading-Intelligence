"""Static and runtime read-only guards for Crypto Intelligence."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.crypto.risk import assert_crypto_read_only
from tests.conftest import make_test_settings

FORBIDDEN = ["place_order", "sign_transaction", "private_key", "wallet.sign"]


def test_crypto_package_has_no_execution_symbols():
    root = Path(__file__).resolve().parents[2] / "app" / "crypto"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.name}:{token}")
    assert offenders == []


def test_assert_crypto_read_only_passes_when_true():
    assert_crypto_read_only(make_test_settings(crypto_intel_read_only=True))


def test_assert_crypto_read_only_fails_when_false():
    with pytest.raises(RuntimeError, match="read-only"):
        assert_crypto_read_only(make_test_settings(crypto_intel_read_only=False))
