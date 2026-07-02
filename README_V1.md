# TI — Trading Intelligence V1.0.1

Polymarket Weather Signal Bot.

## What this version does

- Telegram bot with a clean TI menu.
- English-only interface for now.
- Polymarket only.
- Weather markets only.
- Reads Polymarket market/event metadata first: description, rules, resolution source, outcomes, prices.
- Extracts explicit weather station codes from rules, for example KORD, KJFK, KLAX, KPHX.
- If a station code is found, pulls fresh NOAA/NWS cross-check data from api.weather.gov:
  - station metadata
  - latest observation
  - daily forecast
  - hourly forecast
- Uses Claude/Anthropic with web search to verify fresh forecast and resolution-source evidence.
- Stores users, filters, and analyses in SQLite.

## Important truth

The bot does not guarantee profit.
It does not auto-trade.
If Polymarket rules do not clearly confirm the resolution source or station, the bot must answer WAIT or SKIP.
If the market says Weather Underground, Weather Underground remains the primary source. NOAA/NWS is only a fresh official cross-check unless the market rules name NWS/NOAA/weather.gov.

## Files

- `ti_polymarket_weather_bot_v1.py` — main bot file
- `requirements_v1.txt` — Python dependencies
- `env_example_v1.txt` — example environment config
- `gitignore_v1.txt` — safe Git ignore file

## Setup locally or on VPS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_v1.txt
cp env_example_v1.txt .env
nano .env
python ti_polymarket_weather_bot_v1.py
```

## Required .env values

```env
TELEGRAM_TOKEN=your_new_telegram_bot_token
ANTHROPIC_API_KEY=your_anthropic_api_key
ADMIN_IDS=your_telegram_user_id
```

## Telegram test

1. Send `/start`.
2. Press `☁️ Weather`.
3. Press `🔎 Find weather markets`.
4. Send `/analyze https://polymarket.com/event/...` with a real weather market URL.

## GitHub safety

Never upload `.env` to GitHub.
Use `.env.example` / `env_example_v1.txt` only.
If an old token was ever committed publicly, rotate it in BotFather or Anthropic Console.
