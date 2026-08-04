"""Fee schedule and crypto health probe tests."""

from __future__ import annotations

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.fees import get_spot_fee_schedule
from app.crypto.health import probe_all_exchanges, summarize_exchange_health
from app.crypto.models import ExchangeHealth, HealthStatus
from app.database.storage import initialize_database
from app.health.monitor import probe_crypto_readonly, run_health_check
from tests.conftest import make_test_settings


def test_known_spot_fees():
    result = get_spot_fee_schedule("binance")
    assert isinstance(result, ApiSuccess)
    assert result.data.taker_rate > 0


def test_missing_fee_schedule_fail_closed():
    result = get_spot_fee_schedule("unknown_venue")
    assert isinstance(result, ApiFailure)
    assert result.code == "missing_fee_schedule"


def test_unsupported_market_type_fees():
    result = get_spot_fee_schedule("binance", market_type="perp")  # type: ignore[arg-type]
    assert isinstance(result, ApiFailure)
    assert result.code == "unsupported_market_type"


def test_probe_disabled_exchanges():
    settings = make_test_settings(crypto_intel_enabled=False)
    rows = probe_all_exchanges(settings)
    assert rows
    assert all(r.status == HealthStatus.DISABLED for r in rows)
    assert "disabled=" in summarize_exchange_health(rows)


def test_probe_crypto_readonly_violation(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "c.db"),
        crypto_intel_enabled=True,
        crypto_intel_read_only=False,
    )
    initialize_database(settings)
    status = probe_crypto_readonly(settings)
    assert status.status == "DOWN"
    assert "READ-ONLY" in status.detail


def test_health_check_includes_crypto_components(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "h.db"))
    initialize_database(settings)
    overall = run_health_check(settings, bot_data={})
    names = {c.component for c in overall.components}
    assert "crypto_intel" in names
    assert "crypto_readonly" in names


def test_summarize_mixed_health():
    rows = [
        ExchangeHealth("binance", HealthStatus.OK, "ok", 1, 10.0),
        ExchangeHealth("bybit", HealthStatus.DEGRADED, "slow", 1, 20.0),
    ]
    text = summarize_exchange_health(rows)
    assert "ok=1" in text
    assert "degraded=1" in text
