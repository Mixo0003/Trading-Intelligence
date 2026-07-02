#!/usr/bin/env python3
"""
TI — Trading Intelligence v1.0.1
Polymarket Weather Signal Bot

Scope v1.0:
- Telegram bot with a clean TI-style menu
- English-only interface for now
- Polymarket weather-market discovery
- Polymarket URL/slug parsing
- Polymarket Gamma metadata lookup
- Polymarket CLOB best bid/ask lookup when token IDs are available
- Claude/Anthropic weather-market analysis with web_search
- User filters saved in SQLite
- Basic reports, assistant, community/support placeholders

Security:
- No API keys in code
- Secrets are loaded from .env

This bot does NOT auto-trade and does NOT guarantee profit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anthropic
import requests
from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ============================================================
# CONFIG
# ============================================================

load_dotenv()

APP_NAME = "TI — Trading Intelligence"
BOT_VERSION = "1.0.1-polymarket-weather-resolution-first"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()
ANTHROPIC_WEB_SEARCH_TOOL = os.getenv("ANTHROPIC_WEB_SEARCH_TOOL", "web_search_20260209").strip()

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
RESTRICT_ACCESS = os.getenv("RESTRICT_ACCESS", "false").lower() in {"1", "true", "yes", "y"}
COMMUNITY_URL = os.getenv("COMMUNITY_URL", "").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()
TI_BANNER_PATH = os.getenv("TI_BANNER_PATH", "").strip()

DB_PATH = os.getenv("DB_PATH", "ti_bot.sqlite3").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
MAX_WEATHER_RESULTS = int(os.getenv("MAX_WEATHER_RESULTS", "8"))
DEFAULT_MIN_EDGE_PCT = float(os.getenv("DEFAULT_MIN_EDGE_PCT", "8"))
DEFAULT_MIN_VOLUME = float(os.getenv("DEFAULT_MIN_VOLUME", "0"))

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN in .env")

anthropic_client: Optional[anthropic.Anthropic] = None
if ANTHROPIC_API_KEY:
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ti_polymarket_weather_bot")

# ============================================================
# TEXT / MENU
# ============================================================

BTN_ARBITRAGE = "📊 Arbitrage"
BTN_WEATHER = "☁️ Weather"
BTN_FILTERS = "⚙️ Filters"
BTN_LANGUAGE = "🌍 Language"
BTN_REPORTS = "📈 Reports"
BTN_ASSISTANT = "🤖 Assistant"
BTN_COMMUNITY = "👥 Community"
BTN_SUPPORT = "🆘 Support"
BTN_BACK = "⬅️ Back"

BTN_FIND_WEATHER = "🔎 Find weather markets"
BTN_ANALYZE = "🧠 Analyze market"
BTN_RECENT = "📋 Recent weather markets"

BTN_SPREAD = "📈 Edge"
BTN_VOLUME = "💰 Volume"
BTN_MARKETS = "📊 Markets"
BTN_SCAN_MODE = "🔍 Scan mode"
BTN_ALERTS = "📩 Alerts"
BTN_RESET = "🔄 Reset filters"

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_ARBITRAGE), KeyboardButton(BTN_WEATHER)],
        [KeyboardButton(BTN_FILTERS), KeyboardButton(BTN_LANGUAGE)],
        [KeyboardButton(BTN_REPORTS), KeyboardButton(BTN_ASSISTANT)],
        [KeyboardButton(BTN_COMMUNITY), KeyboardButton(BTN_SUPPORT)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

WEATHER_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_FIND_WEATHER), KeyboardButton(BTN_ANALYZE)],
        [KeyboardButton(BTN_RECENT), KeyboardButton(BTN_BACK)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

FILTERS_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_SPREAD), KeyboardButton(BTN_VOLUME)],
        [KeyboardButton(BTN_MARKETS), KeyboardButton(BTN_SCAN_MODE)],
        [KeyboardButton(BTN_ALERTS), KeyboardButton(BTN_RESET)],
        [KeyboardButton(BTN_BACK)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ============================================================
# DATABASE
# ============================================================

DEFAULT_FILTERS = {
    "language": "en",
    "markets": ["Polymarket"],
    "categories": ["weather"],
    "min_edge_pct": DEFAULT_MIN_EDGE_PCT,
    "min_volume": DEFAULT_MIN_VOLUME,
    "scan_mode": "manual",
    "alerts_enabled": False,
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                filters_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                market_title TEXT,
                market_slug TEXT,
                created_at TEXT NOT NULL,
                result_text TEXT NOT NULL
            )
            """
        )
        conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_user_filters(user_id: int) -> Dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT filters_json FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return dict(DEFAULT_FILTERS)
    try:
        saved = json.loads(row["filters_json"])
        merged = dict(DEFAULT_FILTERS)
        merged.update(saved)
        return merged
    except Exception:
        return dict(DEFAULT_FILTERS)


def upsert_user(update: Update) -> None:
    user = update.effective_user
    if not user:
        return
    uid = user.id
    filters_json = json.dumps(get_user_filters(uid), ensure_ascii=False)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, filters_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                updated_at=excluded.updated_at
            """,
            (uid, user.username or "", user.first_name or "", filters_json, now_iso(), now_iso()),
        )
        conn.commit()


def save_filters(user_id: int, filters_data: Dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, filters_json, created_at, updated_at)
            VALUES (?, '', '', ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                filters_json=excluded.filters_json,
                updated_at=excluded.updated_at
            """,
            (user_id, json.dumps(filters_data, ensure_ascii=False), now_iso(), now_iso()),
        )
        conn.commit()


def save_analysis(user_id: int, market_title: str, market_slug: str, result_text: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO analyses (user_id, market_title, market_slug, created_at, result_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, market_title[:300], market_slug[:200], now_iso(), result_text[:8000]),
        )
        conn.commit()

# ============================================================
# ACCESS
# ============================================================


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_allowed(user_id: int) -> bool:
    if not RESTRICT_ACCESS:
        return True
    return is_admin(user_id)


async def check_access(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not is_allowed(user.id):
        await update.effective_message.reply_text("Access denied.")
        return False
    return True

# ============================================================
# POLYMARKET HELPERS
# ============================================================


def http_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    headers = {
        "User-Agent": "ti-polymarket-weather-bot/1.0",
        "Accept": "application/json",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def parse_jsonish(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def extract_polymarket_slug(text: str) -> Optional[str]:
    text = text.strip()
    match = re.search(r"https?://(?:www\.)?polymarket\.com/(event|market)/([^?\s/#]+)", text, re.I)
    if match:
        return match.group(2).strip()
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{5,}", text.lower()):
        return text.lower()
    return None


def normalize_market(market: Dict[str, Any]) -> Dict[str, Any]:
    outcomes = parse_jsonish(market.get("outcomes") or market.get("outcomeNames") or market.get("tokens"))
    prices = parse_jsonish(market.get("outcomePrices") or market.get("prices") or market.get("lastPrices"))
    clob_token_ids = parse_jsonish(market.get("clobTokenIds") or market.get("tokenIds"))
    return {
        "id": market.get("id") or market.get("conditionId") or market.get("marketId"),
        "slug": market.get("slug") or "",
        "question": market.get("question") or market.get("title") or "Untitled Polymarket market",
        "description": market.get("description") or market.get("resolutionSource") or "",
        "rules": market.get("rules") or market.get("resolutionRules") or "",
        "outcomes": outcomes,
        "outcome_prices": prices,
        "clob_token_ids": clob_token_ids,
        "active": market.get("active"),
        "closed": market.get("closed"),
        "volume": market.get("volume") or market.get("volumeNum"),
        "liquidity": market.get("liquidity") or market.get("liquidityNum"),
        "end_date": market.get("endDate") or market.get("end_date_iso") or market.get("end_date") or "",
        "raw": market,
    }


def normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    markets = event.get("markets") or []
    return {
        "id": event.get("id"),
        "slug": event.get("slug") or "",
        "title": event.get("title") or event.get("question") or "Untitled Polymarket event",
        "description": event.get("description") or "",
        "rules": event.get("rules") or "",
        "active": event.get("active"),
        "closed": event.get("closed"),
        "volume": event.get("volume") or event.get("volumeNum"),
        "liquidity": event.get("liquidity") or event.get("liquidityNum"),
        "markets": [normalize_market(m) for m in markets if isinstance(m, dict)],
        "raw": event,
    }


def fetch_event_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    data = http_get_json(f"{GAMMA_BASE_URL}/events", params={"slug": slug})
    if isinstance(data, list) and data:
        return normalize_event(data[0])
    if isinstance(data, dict):
        if data.get("events"):
            return normalize_event(data["events"][0])
        if data.get("slug"):
            return normalize_event(data)
    return None


def fetch_market_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    data = http_get_json(f"{GAMMA_BASE_URL}/markets", params={"slug": slug})
    if isinstance(data, list) and data:
        return normalize_market(data[0])
    if isinstance(data, dict):
        if data.get("markets"):
            return normalize_market(data["markets"][0])
        if data.get("slug"):
            return normalize_market(data)
    return None


def search_weather_events(query: str = "", limit: int = MAX_WEATHER_RESULTS) -> List[Dict[str, Any]]:
    params = {
        "active": "true",
        "closed": "false",
        "limit": 100,
        "order": "volume_24hr",
        "ascending": "false",
    }
    data = http_get_json(f"{GAMMA_BASE_URL}/events", params=params)
    events_raw: List[Dict[str, Any]]
    if isinstance(data, dict):
        events_raw = data.get("events") or data.get("data") or []
    elif isinstance(data, list):
        events_raw = data
    else:
        events_raw = []

    weather_words = [
        "weather", "temperature", "daily high", "high temperature", "hotter", "colder",
        "rain", "snow", "precipitation", "wind", "hurricane", "storm",
    ]
    query_tokens = [t for t in re.findall(r"[a-zA-Z0-9]+", query.lower()) if len(t) >= 3]

    results: List[Tuple[int, Dict[str, Any]]] = []
    for e in events_raw:
        if not isinstance(e, dict):
            continue
        blob = " ".join(str(e.get(k, "")) for k in ["title", "question", "description", "slug"]).lower()
        weather_score = sum(1 for w in weather_words if w in blob)
        query_score = sum(1 for t in query_tokens if t in blob)
        if weather_score or query_score:
            score = weather_score * 3 + query_score
            results.append((score, normalize_event(e)))

    results.sort(key=lambda x: x[0], reverse=True)
    return [event for _, event in results[:limit]]


def get_best_bid_ask(token_id: str) -> Dict[str, Optional[float]]:
    try:
        data = http_get_json(f"{CLOB_BASE_URL}/book", params={"token_id": token_id})
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        best_bid = max((float(x.get("price")) for x in bids if x.get("price") is not None), default=None)
        best_ask = min((float(x.get("price")) for x in asks if x.get("price") is not None), default=None)
        return {"best_bid": best_bid, "best_ask": best_ask}
    except Exception as exc:
        logger.info("CLOB book lookup failed for token %s: %s", token_id, exc)
        return {"best_bid": None, "best_ask": None}


def enrich_market_prices(market: Dict[str, Any]) -> Dict[str, Any]:
    token_ids = market.get("clob_token_ids")
    outcomes = market.get("outcomes")
    if isinstance(token_ids, str):
        token_ids = [token_ids]
    if isinstance(outcomes, str):
        outcomes = [outcomes]

    enriched = []
    if isinstance(token_ids, list):
        for idx, token in enumerate(token_ids[:8]):
            if not token:
                continue
            enriched.append(
                {
                    "outcome": outcomes[idx] if isinstance(outcomes, list) and idx < len(outcomes) else f"outcome_{idx}",
                    "token_id": str(token),
                    **get_best_bid_ask(str(token)),
                }
            )
    market["clob_prices"] = enriched
    return market




def collect_rule_text_from_selected(selected: Optional[Dict[str, Any]]) -> str:
    """Collect market/event rules and descriptions into one text block.

    This is intentionally deterministic: before Claude analyzes anything, the bot
    reads Polymarket metadata and tries to extract the actual resolution source.
    """
    if not selected:
        return ""
    data = selected.get("data") or {}
    parts: List[str] = []

    def add(label: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            parts.append(f"{label}: {text}")

    selected_type = selected.get("type")
    if selected_type == "event":
        add("event_title", data.get("title"))
        add("event_description", data.get("description"))
        add("event_rules", data.get("rules"))
        for idx, m in enumerate(data.get("markets", [])[:12], 1):
            add(f"market_{idx}_question", m.get("question"))
            add(f"market_{idx}_description", m.get("description"))
            add(f"market_{idx}_rules", m.get("rules"))
            add(f"market_{idx}_end_date", m.get("end_date"))
    elif selected_type == "market":
        add("market_question", data.get("question"))
        add("market_description", data.get("description"))
        add("market_rules", data.get("rules"))
        add("market_end_date", data.get("end_date"))
    elif selected_type == "search_results":
        for idx, e in enumerate(data[:5], 1):
            add(f"event_{idx}_title", e.get("title"))
            add(f"event_{idx}_description", e.get("description"))
            add(f"event_{idx}_rules", e.get("rules"))
    return "\n".join(parts)


def extract_resolution_details(rule_text: str) -> Dict[str, Any]:
    """Extract station/source hints from Polymarket rules/description.

    This does not guess. It only extracts explicit signals present in the market
    metadata, such as KORD/KJFK station codes, Weather Underground, NWS/NOAA,
    airport/location wording, and URLs.
    """
    text = rule_text or ""
    upper = text.upper()
    urls = re.findall(r"https?://[^\s)\]]+", text)

    # US airport/weather station identifiers commonly appear as KORD, KJFK, KLAX, etc.
    station_codes = sorted(set(re.findall(r"\bK[A-Z]{3}\b", upper)))

    sources: List[str] = []
    if re.search(r"weather\s*underground|wunderground", text, re.I):
        sources.append("Weather Underground")
    if re.search(r"\bNWS\b|weather\.gov|national weather service|\bNOAA\b", text, re.I):
        sources.append("NWS/NOAA")
    if re.search(r"airport|aerodrome|station", text, re.I):
        sources.append("station/airport mentioned")

    measurement = ""
    if re.search(r"daily\s+(maximum|max|high)|high\s+temperature|max(?:imum)?\s+temperature", text, re.I):
        measurement = "daily maximum/high temperature"
    elif re.search(r"daily\s+(minimum|min|low)|low\s+temperature|min(?:imum)?\s+temperature", text, re.I):
        measurement = "daily minimum/low temperature"
    elif re.search(r"rain|precipitation|snow|wind|hurricane|storm", text, re.I):
        measurement = "non-temperature weather metric mentioned"

    return {
        "station_codes_explicitly_found": station_codes,
        "sources_explicitly_found": sorted(set(sources)),
        "source_urls_explicitly_found": urls[:10],
        "measurement_hint": measurement or "not explicitly extracted",
        "rules_confirmed": bool(text.strip()),
    }


def fetch_noaa_station_packet(station_code: str) -> Dict[str, Any]:
    """Fetch fresh public NOAA/NWS evidence for an explicit station code.

    Uses api.weather.gov. This is not a substitute for Weather Underground if the
    Polymarket rules resolve specifically through Weather Underground; it is a
    fresh official cross-check. Claude is instructed to respect the Polymarket
    resolution source first.
    """
    station_code = station_code.upper().strip()
    packet: Dict[str, Any] = {"station": station_code, "source": "api.weather.gov", "errors": []}
    headers = {
        "User-Agent": "ti-polymarket-weather-bot/1.0 (contact: admin)",
        "Accept": "application/geo+json, application/json",
    }

    def get(url: str) -> Any:
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    try:
        station = get(f"https://api.weather.gov/stations/{station_code}")
        props = station.get("properties", {}) if isinstance(station, dict) else {}
        packet["station_name"] = props.get("name")
        packet["station_timezone"] = props.get("timeZone")
        coords = ((station.get("geometry") or {}).get("coordinates") or []) if isinstance(station, dict) else []
        if isinstance(coords, list) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
            packet["latitude"] = lat
            packet["longitude"] = lon
        else:
            lat = lon = None
    except Exception as exc:
        packet["errors"].append(f"station lookup failed: {exc}")
        lat = lon = None

    try:
        obs = get(f"https://api.weather.gov/stations/{station_code}/observations/latest")
        props = obs.get("properties", {}) if isinstance(obs, dict) else {}
        packet["latest_observation"] = {
            "timestamp": props.get("timestamp"),
            "textDescription": props.get("textDescription"),
            "temperature_C": (props.get("temperature") or {}).get("value"),
            "windDirection_deg": (props.get("windDirection") or {}).get("value"),
            "windSpeed_kmh": (props.get("windSpeed") or {}).get("value"),
            "relativeHumidity_pct": (props.get("relativeHumidity") or {}).get("value"),
        }
    except Exception as exc:
        packet["errors"].append(f"latest observation failed: {exc}")

    if lat is not None and lon is not None:
        try:
            point = get(f"https://api.weather.gov/points/{lat},{lon}")
            props = point.get("properties", {}) if isinstance(point, dict) else {}
            forecast_url = props.get("forecast")
            hourly_url = props.get("forecastHourly")
            packet["nws_grid"] = {
                "forecastOffice": props.get("forecastOffice"),
                "gridId": props.get("gridId"),
                "gridX": props.get("gridX"),
                "gridY": props.get("gridY"),
                "forecast_url": forecast_url,
                "hourly_url": hourly_url,
            }
            if forecast_url:
                forecast = get(forecast_url)
                periods = ((forecast.get("properties") or {}).get("periods") or []) if isinstance(forecast, dict) else []
                packet["daily_forecast_periods"] = [
                    {
                        "name": p.get("name"),
                        "startTime": p.get("startTime"),
                        "endTime": p.get("endTime"),
                        "temperature": p.get("temperature"),
                        "temperatureUnit": p.get("temperatureUnit"),
                        "windSpeed": p.get("windSpeed"),
                        "windDirection": p.get("windDirection"),
                        "shortForecast": p.get("shortForecast"),
                        "detailedForecast": p.get("detailedForecast"),
                    }
                    for p in periods[:8]
                ]
            if hourly_url:
                hourly = get(hourly_url)
                periods = ((hourly.get("properties") or {}).get("periods") or []) if isinstance(hourly, dict) else []
                packet["hourly_forecast_periods"] = [
                    {
                        "startTime": p.get("startTime"),
                        "temperature": p.get("temperature"),
                        "temperatureUnit": p.get("temperatureUnit"),
                        "windSpeed": p.get("windSpeed"),
                        "windDirection": p.get("windDirection"),
                        "shortForecast": p.get("shortForecast"),
                    }
                    for p in periods[:24]
                ]
        except Exception as exc:
            packet["errors"].append(f"forecast lookup failed: {exc}")

    return packet


def build_resolution_first_weather_packet(selected: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the deterministic evidence packet used before Claude reasoning."""
    rule_text = collect_rule_text_from_selected(selected)
    details = extract_resolution_details(rule_text)
    packet: Dict[str, Any] = {
        "strategy": "resolution_rules_first",
        "rule_text_excerpt": rule_text[:6000],
        "extracted_resolution_details": details,
        "official_weather_cross_checks": [],
        "important_note": (
            "If Polymarket rules specify Weather Underground, the exact Weather Underground "
            "station/source remains primary. NOAA/NWS data is included only as an official "
            "fresh cross-check unless the rules name NWS/NOAA/weather.gov."
        ),
    }
    for station in details.get("station_codes_explicitly_found", [])[:3]:
        packet["official_weather_cross_checks"].append(fetch_noaa_station_packet(station))
    return packet


def build_market_context(user_text: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    slug = extract_polymarket_slug(user_text)
    lines: List[str] = []
    selected: Optional[Dict[str, Any]] = None

    if slug:
        event = fetch_event_by_slug(slug)
        market = fetch_market_by_slug(slug)
        if event:
            selected = {"type": "event", "data": event}
            lines.append("POLYMARKET EVENT FOUND BY SLUG")
            lines.append(f"Event title: {event.get('title')}")
            lines.append(f"Event slug: {event.get('slug')}")
            lines.append(f"Event description: {event.get('description')}")
            lines.append(f"Event rules: {event.get('rules')}")
            lines.append(f"Event volume: {event.get('volume')}")
            lines.append(f"Event liquidity: {event.get('liquidity')}")
            lines.append("Markets:")
            for m in event.get("markets", [])[:12]:
                lines.append(json.dumps(enrich_market_prices(m), ensure_ascii=False, default=str)[:2500])
        elif market:
            market = enrich_market_prices(market)
            selected = {"type": "market", "data": market}
            lines.append("POLYMARKET MARKET FOUND BY SLUG")
            lines.append(json.dumps(market, ensure_ascii=False, default=str)[:5000])
        else:
            lines.append(f"POLYMARKET LOOKUP FAILED FOR SLUG: {slug}")
    else:
        events = search_weather_events(user_text, limit=5)
        selected = {"type": "search_results", "data": events}
        if events:
            lines.append("POLYMARKET WEATHER SEARCH RESULTS")
            for e in events:
                compact = {
                    "title": e.get("title"),
                    "slug": e.get("slug"),
                    "description": e.get("description"),
                    "rules": e.get("rules"),
                    "volume": e.get("volume"),
                    "liquidity": e.get("liquidity"),
                    "markets": [
                        {
                            "question": m.get("question"),
                            "slug": m.get("slug"),
                            "description": m.get("description"),
                            "rules": m.get("rules"),
                            "outcomes": m.get("outcomes"),
                            "outcome_prices": m.get("outcome_prices"),
                            "end_date": m.get("end_date"),
                            "volume": m.get("volume"),
                            "liquidity": m.get("liquidity"),
                        }
                        for m in e.get("markets", [])[:8]
                    ],
                }
                lines.append(json.dumps(compact, ensure_ascii=False, default=str)[:3000])
        else:
            lines.append("NO POLYMARKET WEATHER MARKET FOUND FROM TEXT.")

    return "\n".join(lines), selected

# ============================================================
# CLAUDE ANALYSIS
# ============================================================

WEATHER_SYSTEM_PROMPT = """
You are a professional meteorologist and Polymarket weather-market analyst.

Scope:
- Analyze weather-based Polymarket markets only.
- Use the market's real rules, resolution source, station/source, and measurement window first.
- Then use current weather evidence from web search.
- Then compare against available market prices.

Hard rules:
1. Never invent data, odds, prices, forecasts, sources, or signals.
2. Never use old forecasts from memory.
3. If rules or station are missing, say exactly: "Rules not confirmed; station is an assumption."
4. Do not replace an airport station with downtown weather unless the market rules allow it.
5. Do not guarantee profit.
6. If there is not enough evidence, output SKIP or WAIT.
7. Treat this as research, not financial advice.
8. Respond in English only for v1.0.

Output format:
🚀 TI Weather Market Analysis

Market:
Resolution source:
Station / measurement location:
Measurement window:
Current market prices:
Fresh weather evidence:
Expected outcome range:
Market comparison:
Decision: WATCH / WAIT / SKIP / POSSIBLE EDGE
Reason:
Risks:
Data gaps:

Keep it practical and concise. Include source names/links in text when web search provides them.
"""


def text_from_anthropic_response(response: Any) -> str:
    chunks: List[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            chunks.append(getattr(block, "text", ""))
    return "\n".join(x for x in chunks if x).strip()


def analyze_weather_market(user_text: str, filters_data: Dict[str, Any]) -> Tuple[str, str, str]:
    if anthropic_client is None:
        return (
            "Claude is not configured. Add ANTHROPIC_API_KEY to .env to use weather analysis.",
            "",
            "",
        )

    market_context, selected = build_market_context(user_text)
    resolution_packet = build_resolution_first_weather_packet(selected)
    filter_context = json.dumps(filters_data, ensure_ascii=False)
    resolution_context = json.dumps(resolution_packet, ensure_ascii=False, default=str)
    prompt = f"""
User request:
{user_text}

User filters:
{filter_context}

Polymarket data packet:
{market_context}

Resolution-first weather evidence packet built by code BEFORE AI reasoning:
{resolution_context}

Task:
Analyze the weather market using this exact order:
1. Read Polymarket description/rules/resolution source first.
2. Use only explicit station/source details extracted from those rules. Do not guess a station.
3. If a station code is present, use the NOAA/NWS packet as a fresh official cross-check.
4. Use web_search to verify the resolution source and fresh forecast evidence, especially if Weather Underground is named.
5. Compare the evidence against the market prices.
6. If the station/source cannot be confirmed from rules, say exactly what is missing and choose WAIT/SKIP.
"""

    response = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1800,
        system=WEATHER_SYSTEM_PROMPT,
        tools=[{"type": ANTHROPIC_WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": prompt}],
    )
    result = text_from_anthropic_response(response) or "No analysis text returned by Claude."

    title = ""
    slug = ""
    if selected:
        data = selected.get("data") or {}
        if selected.get("type") == "event":
            title = data.get("title", "")
            slug = data.get("slug", "")
        elif selected.get("type") == "market":
            title = data.get("question", "")
            slug = data.get("slug", "")
    return result, title, slug

# ============================================================
# FORMATTERS
# ============================================================


def intro_text() -> str:
    return (
        "🚀 TI — Trading Intelligence\n\n"
        "Real-time market intelligence for prediction traders.\n\n"
        "v1.0 focus: Polymarket weather markets.\n"
        "Select an option below to begin."
    )


def filters_text(filters_data: Dict[str, Any]) -> str:
    return (
        "⚙️ Filters\n\n"
        f"Markets: {', '.join(filters_data.get('markets', []))}\n"
        f"Category: {', '.join(filters_data.get('categories', []))}\n"
        f"Minimum edge: {filters_data.get('min_edge_pct')}%\n"
        f"Minimum volume: ${filters_data.get('min_volume')}\n"
        f"Scan mode: {filters_data.get('scan_mode')}\n"
        f"Alerts: {'ON' if filters_data.get('alerts_enabled') else 'OFF'}\n\n"
        "Choose what you want to configure."
    )


def format_weather_events(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "No active weather-related Polymarket events were found."
    lines = ["☁️ Recent Polymarket weather markets\n"]
    for idx, e in enumerate(events, 1):
        slug = e.get("slug") or ""
        url = f"https://polymarket.com/event/{slug}" if slug else ""
        lines.append(
            f"{idx}. {e.get('title')}\n"
            f"   Volume: {e.get('volume') or 'unknown'} | Liquidity: {e.get('liquidity') or 'unknown'}\n"
            f"   {url}\n"
        )
    return "\n".join(lines)[:3900]


def split_telegram_text(text: str, max_len: int = 3900) -> List[str]:
    if len(text) <= max_len:
        return [text]
    parts: List[str] = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) > max_len:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts

# ============================================================
# HANDLERS
# ============================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    upsert_user(update)
    if TI_BANNER_PATH and Path(TI_BANNER_PATH).exists():
        try:
            await update.effective_message.reply_photo(photo=open(TI_BANNER_PATH, "rb"), caption=intro_text(), reply_markup=MAIN_MENU)
            return
        except Exception as exc:
            logger.warning("Could not send TI banner: %s", exc)
    await update.effective_message.reply_text(intro_text(), reply_markup=MAIN_MENU)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.effective_message.reply_text("Admin only.")
        return
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        analyses = conn.execute("SELECT COUNT(*) AS c FROM analyses").fetchone()["c"]
    await update.effective_message.reply_text(
        "👑 Admin panel\n\n"
        f"Version: {BOT_VERSION}\n"
        f"Users: {users}\n"
        f"Saved analyses: {analyses}\n"
        f"Anthropic configured: {'yes' if anthropic_client else 'no'}\n"
        f"Model: {ANTHROPIC_MODEL}\n"
        f"Restrict access: {RESTRICT_ACCESS}"
    )


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    upsert_user(update)
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text(
            "Send a Polymarket URL/slug or weather market description.\n\n"
            "Example:\n/analyze https://polymarket.com/event/..."
        )
        return
    await run_analysis(update, text)


async def find_weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    query = " ".join(context.args).strip()
    await update.effective_message.reply_text("🔎 Searching active Polymarket weather markets...")
    try:
        events = search_weather_events(query=query, limit=MAX_WEATHER_RESULTS)
        await update.effective_message.reply_text(format_weather_events(events), reply_markup=WEATHER_MENU)
    except Exception as exc:
        logger.exception("Weather search failed")
        await update.effective_message.reply_text(f"Weather market search failed: {exc}")


async def run_analysis(update: Update, user_text: str) -> None:
    uid = update.effective_user.id
    await update.effective_message.reply_text("🧠 Analyzing with Polymarket data + fresh weather search...")
    try:
        result, title, slug = analyze_weather_market(user_text, get_user_filters(uid))
        save_analysis(uid, title, slug, result)
        for part in split_telegram_text(result):
            await update.effective_message.reply_text(part)
    except Exception as exc:
        logger.exception("Analysis failed")
        await update.effective_message.reply_text(
            "Analysis failed.\n\n"
            f"Error: {exc}\n\n"
            "Check ANTHROPIC_API_KEY, ANTHROPIC_MODEL, and whether your Anthropic account has web_search enabled."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    upsert_user(update)
    text = (update.effective_message.text or "").strip()
    uid = update.effective_user.id

    if text == BTN_BACK:
        await update.effective_message.reply_text(intro_text(), reply_markup=MAIN_MENU)
        return

    if text == BTN_WEATHER:
        await update.effective_message.reply_text(
            "☁️ Weather markets\n\n"
            "You can search active weather markets or paste a Polymarket URL for analysis.",
            reply_markup=WEATHER_MENU,
        )
        return

    if text == BTN_FIND_WEATHER or text == BTN_RECENT:
        await find_weather_command(update, context)
        return

    if text == BTN_ANALYZE:
        await update.effective_message.reply_text(
            "Paste a Polymarket weather market URL/slug, or describe the market.\n\n"
            "Example:\nWill Chicago have a high temperature above 90F tomorrow?",
            reply_markup=WEATHER_MENU,
        )
        return

    if text == BTN_ARBITRAGE:
        await update.effective_message.reply_text(
            "📊 Arbitrage\n\n"
            "v1.0 is focused on Polymarket weather-market research.\n"
            "True cross-exchange arbitrage will be added later after the Polymarket weather workflow is stable."
        )
        return

    if text == BTN_FILTERS:
        await update.effective_message.reply_text(filters_text(get_user_filters(uid)), reply_markup=FILTERS_MENU)
        return

    if text == BTN_SPREAD:
        await update.effective_message.reply_text(
            "📈 Edge filter\n\n"
            "Current default minimum edge is saved in .env as DEFAULT_MIN_EDGE_PCT.\n"
            "Interactive editing will be added in v1.1."
        )
        return

    if text == BTN_VOLUME:
        await update.effective_message.reply_text(
            "💰 Volume filter\n\n"
            "Current default minimum volume is saved in .env as DEFAULT_MIN_VOLUME.\n"
            "Interactive editing will be added in v1.1."
        )
        return

    if text == BTN_MARKETS:
        await update.effective_message.reply_text(
            "📊 Markets\n\n"
            "✅ Polymarket\n\n"
            "Kalshi, Manifold, crypto exchanges and other sources are intentionally disabled in v1.0."
        )
        return

    if text == BTN_SCAN_MODE:
        await update.effective_message.reply_text(
            "🔍 Scan mode\n\n"
            "v1.0 mode: manual.\n"
            "You choose or paste a market. The bot analyzes it.\n\n"
            "Automatic alerts can be added after the manual workflow is stable."
        )
        return

    if text == BTN_ALERTS:
        await update.effective_message.reply_text(
            "📩 Alerts\n\n"
            "Disabled in v1.0 to avoid noisy or false signals.\n"
            "We will add scheduled alerts after the analysis quality is tested."
        )
        return

    if text == BTN_RESET:
        save_filters(uid, dict(DEFAULT_FILTERS))
        await update.effective_message.reply_text("✅ Filters reset.", reply_markup=FILTERS_MENU)
        return

    if text == BTN_LANGUAGE:
        await update.effective_message.reply_text(
            "🌍 Language\n\n"
            "v1.0 is English-only for stability.\n"
            "Ukrainian, Polish, and Russian can be added later without changing the core scanner."
        )
        return

    if text == BTN_REPORTS:
        with db() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM analyses WHERE user_id = ?", (uid,)).fetchone()
        await update.effective_message.reply_text(
            "📈 Reports\n\n"
            f"Saved analyses: {row['c']}\n\n"
            "Detailed performance reports will be added after we collect enough test analyses."
        )
        return

    if text == BTN_ASSISTANT:
        await update.effective_message.reply_text(
            "🤖 Assistant\n\n"
            "Ask me about a Polymarket weather market.\n"
            "For best results, paste the exact Polymarket URL."
        )
        return

    if text == BTN_COMMUNITY:
        if COMMUNITY_URL:
            await update.effective_message.reply_text(f"👥 Community\n\n{COMMUNITY_URL}")
        else:
            await update.effective_message.reply_text("👥 Community\n\nCommunity link is not configured yet.")
        return

    if text == BTN_SUPPORT:
        if SUPPORT_URL:
            await update.effective_message.reply_text(f"🆘 Support\n\n{SUPPORT_URL}")
        else:
            await update.effective_message.reply_text("🆘 Support\n\nSupport link is not configured yet.")
        return

    # Free text fallback: if it looks like a Polymarket/weather request, analyze it.
    lower = text.lower()
    if "polymarket.com" in lower or "weather" in lower or "temperature" in lower or "high" in lower or "rain" in lower or "snow" in lower:
        await run_analysis(update, text)
        return

    await update.effective_message.reply_text(
        "I am v1.0 focused on Polymarket weather markets.\n\n"
        "Paste a Polymarket weather URL or tap ☁️ Weather."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)

# ============================================================
# MAIN
# ============================================================


def main() -> None:
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", status))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("find_weather", find_weather_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    logger.info("Starting %s %s", APP_NAME, BOT_VERSION)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
