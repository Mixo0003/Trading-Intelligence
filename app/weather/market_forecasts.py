from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.config.settings import Settings
from app.polymarket.client import PolymarketClient
from app.polymarket.markets import get_weather_events
from app.polymarket.models import PolymarketEvent, PolymarketMarket
from app.weather.resolution import extract_date_text, extract_resolution_source
from app.weather.station_resolver import resolve_station_from_text
from app.weather.stations import WeatherStation


FORECAST_ONLY = "FORECAST ONLY"
WATCH = "WATCH"
VALIDATED_OPPORTUNITY = "VALIDATED OPPORTUNITY"
INSUFFICIENT_DATA = "SKIP — INSUFFICIENT DATA"
OUTCOME_MAPPING_FAILED = "SKIP — MARKET_OUTCOME_MAPPING_FAILED"


@dataclass(frozen=True)
class ModelValue:
    name: str
    value_f: float
    timezone: str


@dataclass(frozen=True)
class ForecastResult:
    event_id: str | None
    child_market_id: str | None
    market_url: str | None
    city: str
    station: str | None
    station_timezone: str | None
    target_date: str | None
    market_type: str | None
    native_unit: str
    forecast_value: float | None
    expected_low: float | None
    expected_high: float | None
    selected_bucket: str | None
    model_probability: float | None
    live_price: float | None
    status: str
    validation_reason: str
    forecast_models: list[str]
    forecast_gateway: str | None
    confidence: str
    source_divergence: float | None
    provider_spread: float | None
    station_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ForecastResult":
        return cls(**value)

    @property
    def has_forecast(self) -> bool:
        return (
            self.station_verified
            and self.forecast_value is not None
            and self.target_date is not None
            and self.market_type is not None
        )


@dataclass(frozen=True)
class ForecastScan:
    scan_timestamp: str
    discovered_count: int
    analyzed_count: int
    forecast_result_count: int
    actionable_count: int
    skip_reason_counts: dict[str, int]
    results: list[ForecastResult]
    partial_failures: list[str]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["results"] = [item.to_dict() for item in self.results]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ForecastScan":
        return cls(
            scan_timestamp=value["scan_timestamp"],
            discovered_count=int(value["discovered_count"]),
            analyzed_count=int(value["analyzed_count"]),
            forecast_result_count=int(value["forecast_result_count"]),
            actionable_count=int(value["actionable_count"]),
            skip_reason_counts=dict(value.get("skip_reason_counts") or {}),
            results=[ForecastResult.from_dict(item) for item in value.get("results", [])],
            partial_failures=list(value.get("partial_failures") or []),
        )


def _event_text(event: PolymarketEvent) -> str:
    return "\n".join([
        event.title or "",
        event.description,
        *[
            "\n".join([market.question or "", market.description, market.rules])
            for market in event.markets
        ],
    ])


def _parse_target_date(text: str, local_today: date | None = None) -> date | None:
    raw = extract_date_text(text)
    if not raw:
        return None
    cleaned = re.sub(r"(st|nd|rd|th)", "", raw, flags=re.IGNORECASE)
    for pattern in ("%Y-%m-%d", "%B %d, %Y", "%B %d"):
        try:
            parsed = datetime.strptime(cleaned.title(), pattern).date()
            if pattern == "%B %d":
                today = local_today or datetime.now(timezone.utc).date()
                parsed = parsed.replace(year=today.year)
                if parsed < today and (today - parsed).days > 30:
                    parsed = parsed.replace(year=today.year + 1)
            return parsed
        except ValueError:
            continue
    return None


def _market_type(text: str) -> str | None:
    lower = text.lower()
    if any(word in lower for word in ("highest temperature", "daily high", "maximum temperature")):
        return "DAILY_HIGH"
    if any(word in lower for word in ("lowest temperature", "daily low", "minimum temperature")):
        return "DAILY_LOW"
    return None


def _city(text: str) -> str:
    match = re.search(
        r"(?:temperature|high|low)\s+in\s+([A-Za-zÀ-ÿ .'-]+?)\s+on\s+",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip().title()
    return "Unknown city"


def _unit(text: str) -> str:
    return "°C" if re.search(r"°\s*C\b|\bcelsius\b", text, re.IGNORECASE) else "°F"


def _to_native(value_f: float, unit: str) -> float:
    return (value_f - 32.0) * 5.0 / 9.0 if unit == "°C" else value_f


def _fetch_open_meteo_model(
    station: WeatherStation,
    target: date,
    market_type: str,
    model_name: str,
    model_code: str,
    timeout: float = 8.0,
) -> ModelValue | None:
    params: dict[str, Any] = {
        "latitude": station.latitude,
        "longitude": station.longitude,
        "elevation": station.elevation_m,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": station.timezone or "auto",
        "models": model_code,
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
    }
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={key: value for key, value in params.items() if value is not None},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    key = "temperature_2m_max" if market_type == "DAILY_HIGH" else "temperature_2m_min"
    values = daily.get(key) or []
    if target.isoformat() not in dates:
        return None
    index = dates.index(target.isoformat())
    if index >= len(values) or values[index] is None:
        return None
    return ModelValue(
        name=model_name,
        value_f=float(values[index]),
        timezone=str(payload.get("timezone") or station.timezone or "UTC"),
    )


def _fetch_nws_reference(
    settings: Settings,
    station: WeatherStation,
    target: date,
    market_type: str,
    timeout: float = 8.0,
) -> float | None:
    # NWS day/night periods are not calendar-day minimum observations. Using a
    # "Tonight" period can validate the following day's low, so daily lows do
    # not use this source as a strict reference.
    if not station.code.startswith("K") or market_type == "DAILY_LOW":
        return None
    headers = {"User-Agent": "Trading Intelligence Bot", "Accept": "application/geo+json"}
    points = requests.get(
        f"{settings.weather_gov_base_url.rstrip('/')}/points/{station.latitude},{station.longitude}",
        headers=headers,
        timeout=timeout,
    )
    points.raise_for_status()
    forecast_url = points.json()["properties"]["forecast"]
    response = requests.get(forecast_url, headers=headers, timeout=timeout)
    response.raise_for_status()
    periods = response.json().get("properties", {}).get("periods", [])
    want_daytime = market_type == "DAILY_HIGH"
    candidates = []
    for period in periods:
        start = period.get("startTime")
        if not start:
            continue
        local_day = datetime.fromisoformat(start).date()
        if local_day == target and bool(period.get("isDaytime")) == want_daytime:
            candidates.append(float(period["temperature"]))
    if not candidates:
        return None
    return max(candidates) if want_daytime else min(candidates)


def fetch_station_forecasts(
    settings: Settings,
    station: WeatherStation,
    target: date,
    market_type: str,
) -> tuple[list[ModelValue], float | None, list[str]]:
    models: list[ModelValue] = []
    reference: float | None = None
    failures: list[str] = []
    jobs = {
        "GFS": lambda: _fetch_open_meteo_model(station, target, market_type, "GFS", "gfs_seamless"),
        "ECMWF": lambda: _fetch_open_meteo_model(station, target, market_type, "ECMWF", "ecmwf_ifs025"),
        "reference": lambda: _fetch_nws_reference(settings, station, target, market_type),
    }
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(call): name for name, call in jobs.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                value = future.result()
                if name == "reference":
                    reference = value
                elif value is not None:
                    models.append(value)
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}")
    models.sort(key=lambda item: item.name)
    return models, reference, failures


@dataclass(frozen=True)
class _Bucket:
    market: PolymarketMarket
    label: str
    lower: float
    upper: float


def _bucket_from_market(market: PolymarketMarket) -> _Bucket | None:
    label = market.question or market.slug or ""
    range_match = re.search(
        r"(?:between|from)\s+(-?\d+(?:\.\d+)?)\s*(?:-|to|and)\s*"
        r"(-?\d+(?:\.\d+)?)\s*°?\s*[FC]\b",
        label,
        re.IGNORECASE,
    )
    if range_match:
        low, high = float(range_match.group(1)), float(range_match.group(2))
        return _Bucket(market, label, low - 0.5, high + 0.5)
    temperature_values = [
        float(value)
        for value in re.findall(
            r"(-?\d+(?:\.\d+)?)\s*°?\s*[FC]\b",
            label,
            re.IGNORECASE,
        )
    ]
    if not temperature_values:
        return None
    lower_label = label.lower()
    value = temperature_values[0]
    if "or below" in lower_label or "or lower" in lower_label or "less than" in lower_label:
        return _Bucket(market, label, -math.inf, value + 0.5)
    if "or above" in lower_label or "or higher" in lower_label or "more than" in lower_label:
        return _Bucket(market, label, value - 0.5, math.inf)
    return _Bucket(market, label, value - 0.5, value + 0.5)


def _normal_cdf(value: float, mean: float, sigma: float) -> float:
    if math.isinf(value):
        return 0.0 if value < 0 else 1.0
    return 0.5 * (1.0 + math.erf((value - mean) / (sigma * math.sqrt(2.0))))


def _yes_price(market: PolymarketMarket) -> float | None:
    for index, outcome in enumerate(market.outcomes):
        if str(outcome).strip().lower() == "yes" and index < len(market.outcome_prices):
            try:
                price = float(market.outcome_prices[index])
                return price if 0.0 <= price <= 1.0 else None
            except (TypeError, ValueError):
                return None
    return None


def _select_actual_bucket(
    markets: list[PolymarketMarket],
    mean_native: float,
    sigma_native: float,
) -> tuple[_Bucket | None, float | None]:
    candidates: list[tuple[_Bucket, float]] = []
    for market in markets:
        bucket = _bucket_from_market(market)
        if bucket is None:
            continue
        probability = _normal_cdf(bucket.upper, mean_native, sigma_native) - _normal_cdf(
            bucket.lower, mean_native, sigma_native
        )
        candidates.append((bucket, max(0.0, probability)))
    if not candidates:
        return None, None
    return max(candidates, key=lambda item: item[1])


def _sibling_outcomes_coherent(
    markets: list[PolymarketMarket],
    target: date,
    market_type: str,
    unit: str,
) -> bool:
    buckets: list[_Bucket] = []
    for market in markets:
        label = market.question or ""
        if _parse_target_date(label, target) != target:
            return False
        if _market_type(label) != market_type or _unit(label) != unit:
            return False
        bucket = _bucket_from_market(market)
        if bucket is None or _yes_price(market) is None:
            return False
        buckets.append(bucket)
    if not buckets:
        return False
    buckets.sort(key=lambda item: item.lower)
    if buckets[0].lower != -math.inf or buckets[-1].upper != math.inf:
        return False
    return all(
        math.isclose(left.upper, right.lower, abs_tol=0.01)
        for left, right in zip(buckets, buckets[1:])
    )


def analyze_temperature_event(settings: Settings, event: PolymarketEvent) -> ForecastResult:
    text = _event_text(event)
    kind = _market_type(text)
    station = resolve_station_from_text(text)
    try:
        local_today = datetime.now(
            ZoneInfo(station.timezone)
            if station is not None and station.timezone
            else timezone.utc
        ).date()
    except Exception:
        local_today = datetime.now(timezone.utc).date()
    target = _parse_target_date(text, local_today)
    # The native display unit belongs to the live sibling labels. Resolution
    # descriptions can mention conversion units and must not override it.
    unit = _unit("\n".join(market.question or "" for market in event.markets))
    base = {
        "event_id": event.id,
        "child_market_id": None,
        "market_url": f"https://polymarket.com/event/{event.slug}" if event.slug else None,
        "city": _city(text),
        "station": station.code if station else None,
        "station_timezone": station.timezone if station else None,
        "target_date": target.isoformat() if target else None,
        "market_type": kind,
        "native_unit": unit,
        "forecast_value": None,
        "expected_low": None,
        "expected_high": None,
        "selected_bucket": None,
        "model_probability": None,
        "live_price": None,
        "forecast_models": [],
        "forecast_gateway": None,
        "confidence": "LOW",
        "source_divergence": None,
        "provider_spread": None,
        "station_verified": station is not None,
    }
    if target is None or kind is None:
        return ForecastResult(
            **base,
            status=INSUFFICIENT_DATA,
            validation_reason="Event title or target date could not be parsed.",
        )
    if station is None:
        return ForecastResult(
            **base,
            status=INSUFFICIENT_DATA,
            validation_reason="Exact resolution station could not be verified.",
        )
    models, reference, failures = fetch_station_forecasts(settings, station, target, kind)
    if not models:
        return ForecastResult(
            **base,
            status=INSUFFICIENT_DATA,
            validation_reason="No usable station-coordinate forecast model was available.",
        )
    values = [item.value_f for item in models]
    mean_f = sum(values) / len(values)
    spread_f = max(values) - min(values) if len(values) > 1 else None
    uncertainty_f = max(2.0, (spread_f or 0.0) + 1.0)
    mean_native = _to_native(mean_f, unit)
    low_native = _to_native(mean_f - uncertainty_f, unit)
    high_native = _to_native(mean_f + uncertainty_f, unit)
    sigma_native = max(0.8 if unit == "°C" else 1.5, (high_native - low_native) / 3.92)
    bucket, probability = _select_actual_bucket(event.markets, mean_native, sigma_native)
    common = {
        **base,
        "station_timezone": models[0].timezone or station.timezone,
        "forecast_value": round(mean_native, 1),
        "expected_low": round(low_native, 1),
        "expected_high": round(high_native, 1),
        "forecast_models": [item.name for item in models],
        "forecast_gateway": "Open-Meteo",
        "provider_spread": round(_to_native((spread_f or 0.0) + 32, unit) - _to_native(32, unit), 2)
        if spread_f is not None
        else None,
    }
    if bucket is None:
        return ForecastResult(
            **common,
            status=OUTCOME_MAPPING_FAILED,
            validation_reason="Forecast is valid, but no live sibling outcome could be mapped.",
        )
    price = _yes_price(bucket.market)
    divergence_f = abs(mean_f - reference) if reference is not None else None
    agreeing_models = len(models) >= 2 and spread_f is not None and spread_f <= 4.0
    reference_agrees = divergence_f is not None and divergence_f <= 3.0
    resolution_source = extract_resolution_source(text)
    reference_verified = resolution_source in {"weather_gov", "noaa"} and reference_agrees
    outcomes_coherent = _sibling_outcomes_coherent(event.markets, target, kind, unit)
    confidence = "HIGH" if agreeing_models and reference_verified else "MEDIUM" if agreeing_models else "LOW"
    status = FORECAST_ONLY
    reason = "Forecast available; strict betting validation is incomplete."
    # WATCH is informational only. A validated opportunity additionally requires
    # independent reference agreement, coherent sibling mapping, price and edge.
    if agreeing_models and price is not None:
        status = WATCH
        reason = "Models agree and price is live; independent reference validation is incomplete."
    edge = probability - price if probability is not None and price is not None else None
    if (
        agreeing_models
        and reference_verified
        and outcomes_coherent
        and price is not None
        and probability is not None
        and edge is not None
        and edge >= 0.05
        and uncertainty_f <= 5.0
        and 0.0 <= probability <= 1.0
    ):
        status = VALIDATED_OPPORTUNITY
        reason = "Models, reference, live price, uncertainty, and edge validation passed."
    elif len(models) == 1:
        reason = "One station model is usable; no betting recommendation is validated."
    if failures and status != VALIDATED_OPPORTUNITY:
        reason = f"{reason.rstrip('.')} ({', '.join(failures)} unavailable)."
    final = {
        **common,
        "child_market_id": bucket.market.id,
        "selected_bucket": bucket.label,
        "model_probability": round(probability, 4) if probability is not None and len(models) >= 2 else None,
        "live_price": price,
        "status": status,
        "validation_reason": reason,
        "confidence": confidence,
        "source_divergence": (
            round(_to_native((divergence_f or 0.0) + 32, unit) - _to_native(32, unit), 2)
            if divergence_f is not None
            else None
        ),
    }
    return ForecastResult(**final)


def scan_temperature_forecasts(
    settings: Settings,
    discovery_limit: int = 200,
    analysis_limit: int = 12,
) -> ForecastScan:
    events = get_weather_events(PolymarketClient(settings), limit=discovery_limit)
    candidates = [
        event for event in events
        if _market_type(_event_text(event)) is not None
    ][:analysis_limit]
    results: list[ForecastResult] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(analyze_temperature_event, settings, event): event
            for event in candidates
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append(f"{event.id or event.slug}: {type(exc).__name__}")
    results.sort(key=lambda item: (
        not item.has_forecast,
        item.status != VALIDATED_OPPORTUNITY,
        item.status != WATCH,
        item.city,
    ))
    skip_counts: dict[str, int] = {}
    for item in results:
        if item.status.startswith("SKIP"):
            skip_counts[item.validation_reason] = skip_counts.get(item.validation_reason, 0) + 1
    return ForecastScan(
        scan_timestamp=datetime.now(timezone.utc).isoformat(),
        discovered_count=len(events),
        analyzed_count=len(results),
        forecast_result_count=sum(item.has_forecast for item in results),
        actionable_count=sum(item.status == VALIDATED_OPPORTUNITY for item in results),
        skip_reason_counts=skip_counts,
        results=results,
        partial_failures=failures,
    )


def is_target_today(result: ForecastResult, now: datetime | None = None) -> bool:
    if not result.target_date or not result.station_timezone:
        return False
    instant = now or datetime.now(timezone.utc)
    try:
        local_today = instant.astimezone(ZoneInfo(result.station_timezone)).date()
    except Exception:
        return False
    return result.target_date == local_today.isoformat()
