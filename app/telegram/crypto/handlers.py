"""Telegram handlers for live Crypto Intelligence leaves (V3.0.0a/b/c)."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.crypto.arbitrage import collect_arbitrage_opportunities
from app.crypto.health import probe_all_exchanges
from app.crypto.market_data import collect_spread_quotes
from app.crypto.overview import collect_market_overview
from app.crypto.risk import assert_crypto_read_only, crypto_feature_enabled
from app.telegram.crypto.formatting import (
    format_arbitrage_opportunities,
    format_exchanges_health,
    format_market_overview,
    format_spread_monitor,
)
from app.telegram.inline_menus import (
    CB_CRYPTO_ARB,
    CB_CRYPTO_EXCHANGES,
    CB_CRYPTO_MENU,
    CB_CRYPTO_OVERVIEW,
    CB_CRYPTO_SPREAD,
    crypto_arb_keyboard,
    crypto_inline_keyboard,
    crypto_overview_keyboard,
    stub_back_keyboard,
)
from app.telegram.nav import edit_or_reply

logger = logging.getLogger(__name__)

LIVE_CRYPTO_CALLBACKS = frozenset(
    {
        CB_CRYPTO_OVERVIEW,
        CB_CRYPTO_ARB,
        CB_CRYPTO_EXCHANGES,
        CB_CRYPTO_SPREAD,
    }
)


async def handle_crypto_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    settings = context.application.bot_data["settings"]
    back = stub_back_keyboard(
        hub_callback=CB_CRYPTO_MENU, hub_label="Crypto Intelligence"
    )

    try:
        assert_crypto_read_only(settings)
    except RuntimeError:
        await edit_or_reply(
            update,
            "🪙 Crypto Intelligence\n"
            "READ-ONLY SAFETY VIOLATION\n"
            "CRYPTO_INTEL_READ_ONLY must remain true.\n"
            "No market-data calls were made.",
            reply_markup=back,
        )
        return

    enabled = crypto_feature_enabled(settings)

    if data == CB_CRYPTO_OVERVIEW:
        report = collect_market_overview(settings)
        text = format_market_overview(report)
        await edit_or_reply(update, text, reply_markup=crypto_overview_keyboard())
        return

    if data == CB_CRYPTO_ARB:
        report = collect_arbitrage_opportunities(settings)
        text = format_arbitrage_opportunities(report)
        await edit_or_reply(update, text, reply_markup=crypto_arb_keyboard())
        return

    if data == CB_CRYPTO_EXCHANGES:
        rows = probe_all_exchanges(settings)
        text = format_exchanges_health(rows, enabled=enabled)
        await edit_or_reply(update, text, reply_markup=back)
        return

    if data == CB_CRYPTO_SPREAD:
        symbol = settings.crypto_symbols[0] if settings.crypto_symbols else "BTC/USDT"
        if not enabled:
            text = format_spread_monitor(symbol, [], enabled=False)
        else:
            results = collect_spread_quotes(settings, symbol=symbol)
            text = format_spread_monitor(symbol, results, enabled=True)
        await edit_or_reply(update, text, reply_markup=back)
        return

    logger.warning("Unhandled crypto callback: %s", data)
    await edit_or_reply(
        update,
        "🪙 Crypto Intelligence\nUnknown action.",
        reply_markup=crypto_inline_keyboard(),
    )
