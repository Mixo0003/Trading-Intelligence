# Trading Intelligence Architecture

## Main principle

GitHub is the source of truth.
VPS only runs the deployed code.

## V1.0 scope

Only:
- Polymarket
- Weather markets
- Telegram bot interface
- Claude as explanation layer
- Resolution-first strategy

Not included yet:
- Kalshi
- Binance
- Bybit
- Wallet tracking
- Auto-trading

## Module structure

app/main.py — application entry point  
app/telegram/ — Telegram menus, commands, callbacks  
app/polymarket/ — Polymarket API and market data  
app/weather/ — station, forecast, resolution logic  
app/signals/ — signal engine and scoring  
app/assistant/ — Claude integration  
app/database/ — SQLite storage  
app/config/ — settings and environment variables  
app/utils/ — helper functions  
