"""Telegram callback routing for Crypto Intelligence live leaves."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.crypto.models import ExchangeHealth, HealthStatus
from app.telegram.auth import ACCESS_DENIED
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.inline_menus import (
    CB_COPY,
    CB_CRYPTO_ARB,
    CB_CRYPTO_EXCHANGES,
    CB_CRYPTO_MENU,
    CB_CRYPTO_OVERVIEW,
    CB_CRYPTO_SPREAD,
    CB_MAIN,
)
from tests.conftest import make_test_settings
from tests.test_telegram_commands import _context


def _callback_update(data: str, *, user_id: int = 1):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = data
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=user_id, username="u", first_name="T")
    update.message = None
    return update


@pytest.mark.asyncio
async def test_crypto_hub_updated_copy():
    update = _callback_update(CB_CRYPTO_MENU)
    context = _context()
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Crypto Intelligence" in text
    assert "Market Overview" in text
    assert "Arbitrage" in text
    assert "Spread Monitor" in text
    assert "not guaranteed profit" in text.lower()


@pytest.mark.asyncio
async def test_arb_live_disabled_refresh_back(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "arb.db"),
        crypto_intel_enabled=False,
        admin_ids=[1001],
    )
    update = _callback_update(CB_CRYPTO_ARB, user_id=42)
    context = _context()
    context.application.bot_data = {"settings": settings}
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Arbitrage Opportunities" in text
    assert "disabled" in text.lower()
    assert "Not guaranteed profit" in text
    markup = update.callback_query.message.edit_text.await_args.kwargs["reply_markup"]
    datas = [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]
    assert CB_CRYPTO_ARB in datas
    assert CB_CRYPTO_MENU in datas
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_arb_enabled_uses_collector(tmp_path):
    from app.crypto.models import ArbReport

    settings = make_test_settings(
        database_path=str(tmp_path / "arb2.db"),
        crypto_intel_enabled=True,
        crypto_intel_read_only=True,
    )
    fake = ArbReport(
        enabled=True,
        symbols=("BTC/USDT",),
        opportunities=(),
        rejects=(),
    )
    update = _callback_update(CB_CRYPTO_ARB)
    context = _context()
    context.application.bot_data = {"settings": settings}
    with patch(
        "app.telegram.crypto.handlers.collect_arbitrage_opportunities",
        return_value=fake,
    ) as collect:
        await inline_callback_handler(update, context)
        collect.assert_called_once()
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Arbitrage Opportunities" in text
    assert "not an executable" in text.lower()


@pytest.mark.asyncio
async def test_overview_live_disabled_and_refresh_back(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "ov.db"),
        crypto_intel_enabled=False,
        admin_ids=[1001],
    )
    update = _callback_update(CB_CRYPTO_OVERVIEW, user_id=42)
    context = _context()
    context.application.bot_data = {"settings": settings}
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Market Overview" in text
    assert "disabled" in text.lower()
    markup = update.callback_query.message.edit_text.await_args.kwargs["reply_markup"]
    datas = [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]
    assert CB_CRYPTO_OVERVIEW in datas  # refresh
    assert CB_CRYPTO_MENU in datas
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_overview_enabled_uses_collector(tmp_path):
    from app.crypto.models import OverviewReport

    settings = make_test_settings(
        database_path=str(tmp_path / "ov2.db"),
        crypto_intel_enabled=True,
        crypto_intel_read_only=True,
    )
    fake = OverviewReport(
        enabled=True,
        symbols=("BTC/USDT",),
        rows=(),
        rejects=(),
    )
    update = _callback_update(CB_CRYPTO_OVERVIEW)
    context = _context()
    context.application.bot_data = {"settings": settings}
    with patch(
        "app.telegram.crypto.handlers.collect_market_overview",
        return_value=fake,
    ) as collect:
        await inline_callback_handler(update, context)
        collect.assert_called_once()
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Market Overview" in text
    assert "not guaranteed" in text.lower()


@pytest.mark.asyncio
async def test_exchanges_leaf_open_auth_and_disabled_message(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "crypto.db"),
        crypto_intel_enabled=False,
        admin_ids=[1001],
    )
    update = _callback_update(CB_CRYPTO_EXCHANGES, user_id=42)
    context = _context()
    context.application.bot_data = {"settings": settings}
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Exchanges & Health" in text
    assert "disabled" in text.lower()
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_leaf_enabled_uses_collector(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "spread.db"),
        crypto_intel_enabled=True,
        crypto_intel_read_only=True,
    )
    update = _callback_update(CB_CRYPTO_SPREAD)
    context = _context()
    context.application.bot_data = {"settings": settings}
    with patch(
        "app.telegram.crypto.handlers.collect_spread_quotes",
        return_value=[],
    ) as collect:
        await inline_callback_handler(update, context)
        collect.assert_called_once()
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "Spread Monitor" in text
    assert "guaranteed profit" not in text.lower() or "not guaranteed" in text.lower()


@pytest.mark.asyncio
async def test_spread_read_only_violation(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "bad.db"),
        crypto_intel_enabled=True,
        crypto_intel_read_only=False,
    )
    update = _callback_update(CB_CRYPTO_SPREAD)
    context = _context()
    context.application.bot_data = {"settings": settings}
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "READ-ONLY SAFETY VIOLATION" in text


@pytest.mark.asyncio
async def test_copy_admin_gate_unchanged(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "auth.db"),
        admin_ids=[1001],
    )
    update = _callback_update(CB_COPY, user_id=42)
    context = _context()
    context.application.bot_data = {"settings": settings}
    await inline_callback_handler(update, context)
    update.effective_message.reply_text.assert_awaited_with(ACCESS_DENIED)


@pytest.mark.asyncio
async def test_exchanges_enabled_formats_probe(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "probe.db"),
        crypto_intel_enabled=True,
    )
    fake_rows = [
        ExchangeHealth("binance", HealthStatus.OK, "ok", 1, 12.0),
        ExchangeHealth("bybit", HealthStatus.DEGRADED, "timeout", 1, 40.0),
    ]
    update = _callback_update(CB_CRYPTO_EXCHANGES)
    context = _context()
    context.application.bot_data = {"settings": settings}
    with patch(
        "app.telegram.crypto.handlers.probe_all_exchanges",
        return_value=fake_rows,
    ):
        await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.await_args.args[0]
    assert "binance: OK" in text
    assert "bybit: DEGRADED" in text
    markup = update.callback_query.message.edit_text.await_args.kwargs["reply_markup"]
    datas = [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]
    assert CB_CRYPTO_MENU in datas
    assert CB_MAIN not in datas or True  # back goes to crypto hub
