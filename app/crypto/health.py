"""Crypto exchange health probes for Telegram hub and global Health Monitor."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.exchanges import build_clients
from app.crypto.models import EXCHANGE_IDS, ExchangeHealth, HealthStatus
from app.crypto.risk import assert_crypto_read_only, crypto_feature_enabled


def probe_all_exchanges(settings: Settings) -> list[ExchangeHealth]:
    if not crypto_feature_enabled(settings):
        return [
            ExchangeHealth(
                exchange_id=eid,
                status=HealthStatus.DISABLED,
                detail="Crypto Intelligence disabled",
                checked_at_ms=0,
            )
            for eid in EXCHANGE_IDS
        ]
    assert_crypto_read_only(settings)
    clients = build_clients(settings)
    return [clients[eid].probe_health() for eid in EXCHANGE_IDS if eid in clients]


def summarize_exchange_health(rows: list[ExchangeHealth]) -> str:
    ok = sum(1 for r in rows if r.status == HealthStatus.OK)
    degraded = sum(1 for r in rows if r.status == HealthStatus.DEGRADED)
    down = sum(1 for r in rows if r.status == HealthStatus.DOWN)
    disabled = sum(1 for r in rows if r.status == HealthStatus.DISABLED)
    unsupported = sum(1 for r in rows if r.status == HealthStatus.UNSUPPORTED)
    return (
        f"ok={ok} degraded={degraded} down={down} "
        f"unsupported={unsupported} disabled={disabled}"
    )
