"""Checkpoint 7 — opportunity configuration flags."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.config.settings import Settings, load_settings
from tests.conftest import make_test_settings


def test_disabled_defaults_preserve_production_behavior():
    settings = make_test_settings()
    assert settings.opportunity_scanner_enabled is False
    assert settings.opportunity_auto_alerts_enabled is False
    assert settings.opportunity_user_delivery_enabled is False
    assert settings.opportunity_scan_interval_seconds == 120
    assert settings.opportunity_discovery_ttl_seconds == 300
    assert settings.opportunity_max_markets == 100
    assert settings.opportunity_max_books_per_cycle == 40
    assert settings.opportunity_max_live_books_per_cycle == 10
    assert settings.opportunity_concurrency == 5
    assert settings.opportunity_cycle_deadline_seconds == 75
    assert settings.opportunity_first_run_delay_seconds == 30


def test_valid_overrides_via_env():
    env = {
        "TELEGRAM_TOKEN": "t",
        "OPPORTUNITY_SCANNER_ENABLED": "true",
        "OPPORTUNITY_AUTO_ALERTS_ENABLED": "true",
        "OPPORTUNITY_USER_DELIVERY_ENABLED": "true",
        "OPPORTUNITY_SCAN_INTERVAL_SECONDS": "90",
        "OPPORTUNITY_DISCOVERY_TTL_SECONDS": "120",
        "OPPORTUNITY_MAX_MARKETS": "50",
        "OPPORTUNITY_MAX_BOOKS_PER_CYCLE": "20",
        "OPPORTUNITY_MAX_LIVE_BOOKS_PER_CYCLE": "5",
        "OPPORTUNITY_CONCURRENCY": "3",
        "OPPORTUNITY_CYCLE_DEADLINE_SECONDS": "60",
        "OPPORTUNITY_FIRST_RUN_DELAY_SECONDS": "10",
    }
    with patch.dict(os.environ, env, clear=False):
        settings = load_settings()
    assert settings.opportunity_scanner_enabled is True
    assert settings.opportunity_auto_alerts_enabled is True
    assert settings.opportunity_user_delivery_enabled is True
    assert settings.opportunity_scan_interval_seconds == 90
    assert settings.opportunity_max_markets == 50
    assert settings.opportunity_concurrency == 3


@pytest.mark.parametrize(
    "key,value",
    [
        ("OPPORTUNITY_SCAN_INTERVAL_SECONDS", "5"),
        ("OPPORTUNITY_MAX_MARKETS", "101"),
        ("OPPORTUNITY_MAX_BOOKS_PER_CYCLE", "41"),
        ("OPPORTUNITY_MAX_LIVE_BOOKS_PER_CYCLE", "11"),
        ("OPPORTUNITY_CONCURRENCY", "6"),
        ("OPPORTUNITY_CYCLE_DEADLINE_SECONDS", "5"),
        ("OPPORTUNITY_FIRST_RUN_DELAY_SECONDS", "-1"),
    ],
)
def test_invalid_ranges_fail_validation(key, value):
    env = {"TELEGRAM_TOKEN": "t", key: value}
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(ValueError):
            load_settings()


def test_no_secret_or_trading_fields_on_settings():
    fields = set(Settings.__dataclass_fields__)
    banned = {
        "opportunity_private_key",
        "opportunity_wallet",
        "opportunity_api_secret",
        "opportunity_trading_enabled",
        "opportunity_order_placement",
    }
    assert fields.isdisjoint(banned)
    source = open("app/config/settings.py", encoding="utf-8").read()
    assert "OPPORTUNITY_PRIVATE" not in source
    assert "OPPORTUNITY_WALLET" not in source
    assert "OPPORTUNITY_TRADING" not in source
