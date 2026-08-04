"""Read-only / paper-only safety gates for Crypto Intelligence."""

from __future__ import annotations

from app.config.settings import Settings


def assert_crypto_read_only(settings: Settings) -> None:
    if not settings.crypto_intel_read_only:
        raise RuntimeError(
            "Crypto Intelligence must run in read-only mode (CRYPTO_INTEL_READ_ONLY=true)."
        )


def crypto_feature_enabled(settings: Settings) -> bool:
    return bool(settings.crypto_intel_enabled)
