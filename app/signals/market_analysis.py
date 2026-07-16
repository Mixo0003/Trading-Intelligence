from app.config.settings import Settings
from app.ev.policy import MAX_BANKROLL_WEATHER
from app.polymarket.city import extract_city_from_text, is_priority_us_city
from app.polymarket.market_text import build_market_context_text
from app.polymarket.market_type import (
    NormalizedTemperatureType,
    TemperatureMarketType,
    ThresholdDirection,
    classify_temperature_market,
    normalize_temperature_type,
    normalized_type_label,
)
from app.polymarket.models import PolymarketMarket
from app.polymarket.pricing import extract_yes_price, market_polymarket_url
from app.signals.bracket import select_best_outcome
from app.signals.comparison import build_comparison_text
from app.signals.confidence import calculate_basic_confidence
from app.signals.engine import analyze_weather_market
from app.signals.generator import WeatherSignal, generate_weather_signal_message
from app.signals.outcome_labels import build_best_outcome_label, extract_bracket_bounds, extract_exact_temperature
from app.signals.price_validation import (
    extreme_edge_requires_validation,
    price_skip_reason,
    validate_actionable_price,
)
from app.signals.temperature_analysis import (
    estimate_market_probability,
    forecast_in_market_unit,
    threshold_in_unit,
)
from app.metrics.scan_metrics import ScanMetrics
from app.weather.forecast import fetch_weather_gov_forecast
from app.weather.resolution import parse_resolution_details
from app.weather.station_resolver import resolve_station_from_text
from app.weather.temperature_extract import extract_forecast_temperature_f
from app.weather.temperature_units import (
    TemperatureUnit,
    detect_market_unit,
    format_temperature,
    provider_unit_for_forecast,
)

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class MarketAnalysis:
    market_title: str
    market_url: str
    city_location: str
    station_code: str | None
    resolution_source: str | None
    target_date: str | None
    market_type: TemperatureMarketType
    threshold_f: float | None
    threshold_unit: str
    yes_price: float | None
    implied_probability: float | None
    model_probability: float
    edge: float | None
    ev: float | None
    kelly_fraction: float | None
    kelly_cap: float
    confidence: str
    recommendation_tier: str
    reason: str
    data_sources: list[str]
    analyzed_at: str
    message: str
    confidence_score: float = 0.0
    best_outcome: str = "N/A"
    forecast_display: str = ""
    liquidity: float | None = None
    is_priority_us_city: bool = False
    event_id: str | None = None
    market_id: str | None = None
    temperature_type: NormalizedTemperatureType = NormalizedTemperatureType.EXACT_TEMPERATURE
    temperature_type_label: str = ""
    market_unit: str = TemperatureUnit.FAHRENHEIT.value
    provider_unit: str = TemperatureUnit.FAHRENHEIT.value
    displayed_unit: str = TemperatureUnit.FAHRENHEIT.value
    conversion_applied: bool = False
    market_unit_source: str = ""
    raw_yes_price: float | None = None
    price_valid: bool = False
    comparison_text: str = ""
    seasonal_context: str | None = None
    bracket_low: float | None = None
    bracket_high: float | None = None
    forecast_value: float | None = None
    forecast_source: str | None = None
    expected_range_display: str | None = None
    selected_statistic: str | None = None
    validation_error: str | None = None
    resolution_station: str | None = None
    forecast_providers: str | None = None
    provider_spread_display: str | None = None
    forecast_diagnostics: str | None = None
    display_status: str = "SKIP — INSUFFICIENT DATA"
    forecast_confidence: str = "LOW"
    forecast_gateway: str | None = None
    forecast_models: str | None = None
    source_divergence_display: str | None = None
    betting_skip_reason: str | None = None
    station_timezone: str | None = None
    target_date_local: str | None = None
    display_reason: str = ""
    band_rows: tuple[tuple[str, float, float, float | None], ...] = ()
    live_triggers: tuple[str, ...] = ()
    forecast_sources_line: str = ""


@dataclass
class ScanProgress:
    discovered: int = 0
    accepted: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    results: list[MarketAnalysis] = field(default_factory=list)
    all_analyses: list[MarketAnalysis] = field(default_factory=list)
    worldwide_discovered: int = 0  # Legacy direct-market endpoint count.
    direct_temperature_markets_discovered: int = 0
    priority_us_discovered: int = 0
    regional_count: int = 0  # Legacy alias for parent_events_in_region.
    parent_events_discovered: int = 0
    child_markets_discovered: int = 0
    parent_events_in_region: int = 0
    child_markets_loaded_for_analysis: int = 0
    child_markets_analyzed: int = 0
    parent_events_analyzed: int = 0
    scan_region: str = "ALL"
    scan_duration_seconds: float = 0.0
    metrics: ScanMetrics | None = None
    partial: bool = False
    deadline_reached: bool = False
    max_concurrency_observed: int = 0
    weather_com_401_count: int = 0
    polymarket_429_count: int = 0
    incomplete_parent_events: int = 0


def _build_reason(signal_tier: str, edge: float | None, confidence: str) -> str:
    if signal_tier == "SKIP":
        return "Insufficient verified resolution or forecast data."
    if edge is None:
        return "Market price unavailable for EV calculation."
    if signal_tier == "WATCH":
        return "EV below actionable threshold or confidence limits sizing."
    return f"Model edge {edge * 100:+.1f}% with {confidence} confidence."


def _event_id(market: PolymarketMarket) -> str | None:
    events = market.raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = events[0].get("id")
        return str(event_id) if event_id is not None else None
    raw_id = market.raw.get("eventId") or market.raw.get("event_id")
    return str(raw_id) if raw_id is not None else None


def _resolve_best_outcome(
    market: PolymarketMarket,
    normalized_type: NormalizedTemperatureType,
    market_unit: TemperatureUnit,
    context_text: str,
    threshold_display: float | None,
    model_probability: float,
    yes_price: float | None,
) -> str:
    label = build_best_outcome_label(
        market,
        normalized_type,
        market_unit,
        context_text=context_text,
        threshold_display=threshold_display,
    )
    if label:
        return label
    fallback, _ = select_best_outcome(model_probability, yes_price)
    return fallback


def analyze_market(settings: Settings, market: PolymarketMarket) -> MarketAnalysis:
    text = build_market_context_text(market)
    resolution = parse_resolution_details(text)
    market_type, direction = classify_temperature_market(text)
    normalized_type = normalize_temperature_type(text)
    type_label = normalized_type_label(normalized_type)

    market_unit, unit_source = detect_market_unit(market, context_text=text)
    raw_yes_price = extract_yes_price(market)
    price_valid, _ = validate_actionable_price(raw_yes_price)

    station = resolve_station_from_text(text)
    forecast = None
    provider_unit = TemperatureUnit.FAHRENHEIT
    if station is not None:
        forecast = fetch_weather_gov_forecast(settings, station)
        if forecast is not None:
            provider_unit = provider_unit_for_forecast(forecast.source)

    forecast_temperature_f = None
    if forecast is not None:
        forecast_temperature_f = extract_forecast_temperature_f(
            forecast=forecast,
            target_date_text=resolution.target_date,
            market_type=market_type,
            direction=direction,
        )

    model_probability, bracket_low_f, bracket_high_f = estimate_market_probability(
        text=text,
        forecast_f=forecast_temperature_f,
        threshold_f=resolution.threshold_f,
        normalized_type=normalized_type,
        direction=direction,
        market_unit=market_unit,
    )

    confidence_score = calculate_basic_confidence(
        has_station=bool(resolution.station_code),
        has_resolution_source=bool(resolution.source),
        has_forecast=forecast is not None,
        has_threshold=resolution.threshold_f is not None,
    )

    confidence_label = "HIGH" if confidence_score >= 0.75 else "MEDIUM" if confidence_score >= 0.5 else "LOW"

    signal = analyze_weather_market(settings, market, forecast_temperature_f=forecast_temperature_f)
    if price_valid and forecast_temperature_f is not None:
        signal = generate_weather_signal_message(
            market_title=market.question or market.slug or "Unknown Polymarket weather market",
            station_code=resolution.station_code,
            resolution_source=resolution.source,
            threshold_f=resolution.threshold_f,
            forecast_temperature_f=forecast_temperature_f,
            model_probability=model_probability,
            confidence_score=confidence_score,
            market_price=raw_yes_price,
        )
    else:
        skip_reason = (
            price_skip_reason(None)
            if not price_valid
            else "Missing forecast temperature."
        )
        signal = WeatherSignal(
            decision="SKIP",
            probability=model_probability,
            confidence_score=confidence_score,
            confidence_label=confidence_label,
            edge=None,
            ev=None,
            kelly_fraction=None,
            recommendation_tier="SKIP",
            market_probability=raw_yes_price,
            message=f"⚠️ WEATHER MARKET SKIPPED\n\nReason:\n• {skip_reason}",
            forecast_temperature_f=forecast_temperature_f,
        )

    recommendation_tier = signal.recommendation_tier or signal.decision
    if not price_valid or forecast_temperature_f is None:
        recommendation_tier = "SKIP"
    elif extreme_edge_requires_validation(signal.edge, raw_yes_price):
        valid, _ = validate_actionable_price(raw_yes_price)
        if not valid:
            recommendation_tier = "SKIP"

    forecast_value, conversion_applied = forecast_in_market_unit(
        forecast_temperature_f,
        provider_unit,
        market_unit,
    )

    threshold_display = threshold_in_unit(resolution.threshold_f, market_unit)
    bracket_low, bracket_high = extract_bracket_bounds(text, market_unit)
    if normalized_type in {
        NormalizedTemperatureType.ABOVE_THRESHOLD,
        NormalizedTemperatureType.BELOW_THRESHOLD,
        NormalizedTemperatureType.EXACT_TEMPERATURE,
    }:
        bracket_low, bracket_high = None, None
    elif bracket_low is None and bracket_low_f is not None and bracket_high_f is not None:
        bracket_low = threshold_in_unit(bracket_low_f, market_unit)
        bracket_high = threshold_in_unit(bracket_high_f, market_unit)

    best_outcome = _resolve_best_outcome(
        market,
        normalized_type,
        market_unit,
        text,
        threshold_display,
        model_probability,
        raw_yes_price,
    )

    exact_value = extract_exact_temperature(text)
    if best_outcome and "–" not in best_outcome and "-" not in best_outcome:
        bracket_low, bracket_high = None, None

    comparison_type = normalized_type
    comparison_threshold = threshold_display
    if exact_value is not None and bracket_low is None:
        comparison_threshold = exact_value
        if normalized_type in {
            NormalizedTemperatureType.DAILY_HIGH,
            NormalizedTemperatureType.DAILY_LOW,
        }:
            comparison_type = NormalizedTemperatureType.EXACT_TEMPERATURE

    comparison_text = build_comparison_text(
        forecast_value=forecast_value,
        market_unit=market_unit,
        normalized_type=comparison_type,
        threshold_value=comparison_threshold,
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        best_outcome=best_outcome,
    )

    city = extract_city_from_text(text, resolution.station_code)
    sources = []
    if resolution.source:
        sources.append(resolution.source)
    if resolution.station_code:
        sources.append(f"station:{resolution.station_code}")
    sources.append("app/ev engine")

    reason = _build_reason(recommendation_tier, signal.edge, signal.confidence_label)
    if not price_valid:
        reason = price_skip_reason(None)

    return MarketAnalysis(
        market_title=market.question or market.slug or "Unknown market",
        market_url=market_polymarket_url(market),
        city_location=city,
        station_code=resolution.station_code,
        resolution_source=resolution.source,
        target_date=resolution.target_date,
        market_type=market_type,
        threshold_f=resolution.threshold_f,
        threshold_unit=market_unit.value,
        yes_price=raw_yes_price,
        implied_probability=signal.market_probability,
        model_probability=model_probability,
        edge=signal.edge,
        ev=signal.ev,
        kelly_fraction=signal.kelly_fraction,
        kelly_cap=MAX_BANKROLL_WEATHER,
        confidence=signal.confidence_label,
        recommendation_tier=recommendation_tier,
        reason=reason,
        data_sources=sources,
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message=signal.message,
        confidence_score=signal.confidence_score,
        best_outcome=best_outcome,
        forecast_display=format_temperature(forecast_value, market_unit),
        liquidity=market.liquidity,
        is_priority_us_city=is_priority_us_city(
            city, resolution.station_code, settings.priority_weather_cities
        ),
        event_id=_event_id(market),
        market_id=market.id,
        temperature_type=normalized_type,
        temperature_type_label=type_label,
        market_unit=market_unit.value,
        provider_unit=provider_unit.value,
        displayed_unit=market_unit.value,
        conversion_applied=conversion_applied,
        market_unit_source=unit_source,
        raw_yes_price=raw_yes_price,
        price_valid=price_valid,
        comparison_text=comparison_text,
        seasonal_context=None,
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        forecast_value=forecast_value,
    )
