"""Event-level temperature market analysis with coherent sibling outcomes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config.settings import Settings
from app.ev import determine_recommendation_tier
from app.ev.policy import MAX_BANKROLL_WEATHER
from app.ev.types import ConfidenceLevel, MarketCategory, RecommendationTier
from app.polymarket.city import extract_city_from_text, is_priority_us_city
from app.polymarket.event_type import detect_event_temperature_type, validate_event_type_consistency
from app.polymarket.market_text import build_market_context_text
from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType, normalized_type_label
from app.polymarket.models import PolymarketMarket
from app.polymarket.pricing import market_polymarket_url
from app.polymarket.sibling_outcomes import (
    OutcomeKind,
    SiblingOutcome,
    bucket_center,
    outcome_labels,
    parse_sibling_outcomes,
)
from app.signals.bucket_probability import (
    OutcomeProbability,
    default_sigma,
    expected_range,
)
from app.signals.probability_distribution import DistributionContext, distribute_market_probabilities
from app.signals.confidence import calculate_basic_confidence, confidence_label
from app.signals.live_triggers import build_live_triggers
from app.signals.forecast_display import (
    compute_display_status,
    forecast_confidence_level,
    format_forecast_gateway_models,
    format_source_divergence,
    pick_display_outcome,
    short_display_reason,
)
from app.signals.market_analysis import MarketAnalysis
from app.signals.price_validation import validate_actionable_price
from app.weather.daily_forecast import DailyForecastStats, fetch_daily_forecast_stats
from app.weather.evidence import gather_station_evidence
from app.weather.evidence.types import StationEvidence
from app.weather.resolution import parse_resolution_details
from app.weather.sources.common import parse_target_date
from app.weather.station_resolver import (
    extract_resolution_url_from_text,
    resolve_verified_station_from_text,
)
from app.weather.temperature_units import (
    TemperatureUnit,
    celsius_to_fahrenheit,
    convert_temperature,
    detect_market_unit,
    format_temperature,
    unit_symbol,
)

logger = logging.getLogger(__name__)

EXTREME_EDGE_THRESHOLD = 0.40
LOW_PRICE_HIGH_PROB_PRICE = 0.01
LOW_PRICE_HIGH_PROB_MODEL = 0.50
HIGH_EXACT_PROB = 0.70
HIGH_BRACKET_PROB = 0.60
DISTANT_BUCKET_DEGREES = 4.0
NARROW_SIGMA_F = 2.5
NARROW_SIGMA_C = 1.5
MATERIAL_SPREAD_F = 3.0
MATERIAL_SPREAD_C = 1.7


def _spread_native(spread_c: float, unit: TemperatureUnit) -> float:
    if spread_c <= 0:
        return 0.0
    if unit == TemperatureUnit.FAHRENHEIT:
        return spread_c * 9.0 / 5.0
    return spread_c


def _sigma_with_spread(base_sigma: float, spread_c: float, unit: TemperatureUnit) -> float:
    spread_native = _spread_native(spread_c, unit)
    if spread_native <= 0:
        return base_sigma
    return base_sigma + spread_native * 0.35


def _ensemble_confidence_score(
    base_score: float,
    forecast_stats: DailyForecastStats | None,
) -> float:
    if forecast_stats is None:
        return base_score * 0.5
    score = base_score
    if forecast_stats.ensemble_provider_count < 3:
        score -= 0.35
    elif forecast_stats.ensemble_provider_count < 2:
        score -= 0.25
    if forecast_stats.provider_spread_c > MATERIAL_SPREAD_C:
        score -= 0.30
    if (
        forecast_stats.grid_distance_km is not None
        and forecast_stats.grid_distance_km > 5.0
    ):
        score -= 0.10
    return max(0.0, min(score, 1.0))


def _validate_child_market_mapping(
    selected: SiblingOutcome | None,
    selected_prob: OutcomeProbability | None,
    market_price: float | None,
) -> tuple[bool, str | None]:
    if selected is None or selected_prob is None:
        return False, "CHILD_MARKET_MAPPING_MISMATCH"
    if selected.market_id != selected_prob.outcome.market_id:
        return False, "CHILD_MARKET_MAPPING_MISMATCH"
    if selected.label != selected_prob.outcome.label:
        return False, "CHILD_MARKET_MAPPING_MISMATCH"
    if selected.yes_price is not None and market_price is not None:
        if selected.yes_price != market_price:
            return False, "CHILD_MARKET_MAPPING_MISMATCH"
    return True, None


def _validate_forecast_in_sibling_range(
    forecast_center: float,
    sibling_outcomes: list[SiblingOutcome],
    *,
    unit: TemperatureUnit,
) -> tuple[bool, str | None]:
    centers = [
        bucket_center(item)
        for item in sibling_outcomes
        if bucket_center(item) is not None
    ]
    if not centers:
        return True, None
    margin = 20.0 if unit == TemperatureUnit.FAHRENHEIT else 10.0
    low = min(centers) - margin
    high = max(centers) + margin
    if forecast_center < low or forecast_center > high:
        return False, "PROBABILITY_DISTRIBUTION_INCONSISTENT"
    return True, None


def _validate_probability_distribution(
    sibling_probs: list[OutcomeProbability],
    forecast_center: float,
    *,
    sigma: float,
    unit: TemperatureUnit,
) -> tuple[bool, str | None]:
    bracket_items = [
        item for item in sibling_probs
        if item.outcome.kind in {OutcomeKind.BRACKET, OutcomeKind.EXACT}
        and bucket_center(item.outcome) is not None
    ]
    if not bracket_items:
        return True, None

    centers = [
        (bucket_center(item.outcome), item.probability, item)
        for item in bracket_items
        if bucket_center(item.outcome) is not None
    ]
    if not centers:
        return True, None

    near = [
        prob for center, prob, _ in centers
        if abs(center - forecast_center) <= DISTANT_BUCKET_DEGREES
    ]
    max_near = max(near) if near else 0.0

    narrow_sigma = NARROW_SIGMA_F if unit == TemperatureUnit.FAHRENHEIT else NARROW_SIGMA_C
    for center, prob, item in centers:
        distance = abs(center - forecast_center)
        if prob > HIGH_BRACKET_PROB and sigma > narrow_sigma:
            return False, "PROBABILITY_DISTRIBUTION_INCONSISTENT"
        if distance > DISTANT_BUCKET_DEGREES and prob > max_near + 0.01:
            return False, "PROBABILITY_DISTRIBUTION_INCONSISTENT"
        if prob > 0.85 and distance > DISTANT_BUCKET_DEGREES:
            return False, "PROBABILITY_DISTRIBUTION_INCONSISTENT"

    return True, None


def _event_metadata(children: list[PolymarketMarket]) -> tuple[str | None, str | None, str | None]:
    for market in children:
        events = market.raw.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            event = events[0]
            return (
                str(event.get("id") or "") or None,
                event.get("title") or event.get("question"),
                event.get("description"),
            )

    for market in children:
        text = "\n".join([
            market.question or "",
            market.description or "",
            market.rules or "",
        ]).strip()
        if not text:
            continue
        from app.polymarket.event_type import detect_event_temperature_type

        if detect_event_temperature_type(text):
            return None, market.question or market.description, market.description

    return None, None, None


def _structural_validation_passed(
    *,
    event_type: NormalizedTemperatureType,
    type_valid: bool,
    forecast_stats: DailyForecastStats | None,
    selected: SiblingOutcome | None,
    sibling_probs: list[OutcomeProbability],
    model_probability: float | None,
    market_price: float | None,
    edge: float | None,
    active_labels: set[str],
) -> tuple[bool, str | None]:
    if not type_valid:
        return False, "MARKET_TYPE_MISMATCH"
    if forecast_stats is None:
        return False, "FORECAST_UNAVAILABLE"
    if forecast_stats.skip_reason:
        return False, forecast_stats.skip_reason
    if forecast_stats.betting_skip_reason:
        return False, forecast_stats.betting_skip_reason
    if selected is None:
        return False, "OUTCOME_MAPPING_MISMATCH"
    if selected.label.strip().lower() not in active_labels:
        return False, "OUTCOME_MAPPING_MISMATCH"
    if model_probability is None:
        return False, "PROBABILITY_UNAVAILABLE"

    price_valid, _ = validate_actionable_price(market_price)
    if not price_valid:
        return False, "INVALID_PRICE"

    prob_sum = sum(item.probability for item in sibling_probs)
    if not (0.95 <= prob_sum <= 1.05):
        return False, "INCOHERENT_SIBLING_PROBABILITIES"

    if market_price is not None and market_price < LOW_PRICE_HIGH_PROB_PRICE and model_probability > LOW_PRICE_HIGH_PROB_MODEL:
        return False, "LOW_PRICE_HIGH_PROBABILITY_MISMATCH"

    if edge is not None and abs(edge) > EXTREME_EDGE_THRESHOLD:
        if forecast_stats.selected_statistic != "daily_max" and event_type == NormalizedTemperatureType.DAILY_HIGH:
            return False, "FORECAST_STATISTIC_MISMATCH"
        if forecast_stats.selected_statistic != "daily_min" and event_type == NormalizedTemperatureType.DAILY_LOW:
            return False, "FORECAST_STATISTIC_MISMATCH"

    if selected.kind.value == "exact" and model_probability > HIGH_EXACT_PROB:
        return True, None  # tier capped downstream, not a hard skip

    return True, None


def _outcome_near_forecast(
    outcome: SiblingOutcome,
    forecast_native: float,
    *,
    unit: TemperatureUnit,
) -> bool:
    center = bucket_center(outcome)
    if center is not None:
        return abs(center - forecast_native) <= DISTANT_BUCKET_DEGREES
    if outcome.kind == OutcomeKind.BELOW_TAIL and outcome.upper_bound is not None:
        return outcome.upper_bound >= forecast_native - DISTANT_BUCKET_DEGREES
    if outcome.kind == OutcomeKind.ABOVE_TAIL and outcome.lower_bound is not None:
        return outcome.lower_bound <= forecast_native + DISTANT_BUCKET_DEGREES
    return False


def _cap_tier(
    tier: str,
    confidence: str,
    *,
    model_probability: float | None,
    selected_kind: str | None,
    forecast_stats: DailyForecastStats | None,
) -> str:
    if forecast_stats is not None:
        if forecast_stats.ensemble_provider_count < 3:
            return "WATCH" if tier != "SKIP" else "SKIP"
        if forecast_stats.ensemble_provider_count < 2:
            return "WATCH" if tier != "SKIP" else "SKIP"
        if forecast_stats.provider_spread_c > MATERIAL_SPREAD_C:
            if tier == RecommendationTier.STRONG_OPPORTUNITY.value:
                return RecommendationTier.NORMAL_BET.value
    if tier == RecommendationTier.STRONG_OPPORTUNITY.value:
        if confidence != "HIGH":
            return RecommendationTier.NORMAL_BET.value
        if selected_kind == "exact" and model_probability is not None and model_probability > HIGH_EXACT_PROB:
            return RecommendationTier.NORMAL_BET.value
        return RecommendationTier.NORMAL_BET.value
    return tier


def _delta_display(delta_c: float | None, unit: TemperatureUnit) -> str:
    if delta_c is None:
        return "n/a"
    if unit == TemperatureUnit.FAHRENHEIT:
        return f"{delta_c * 9.0 / 5.0:.1f}{unit_symbol(unit)}"
    return f"{delta_c:.1f}{unit_symbol(unit)}"


def _format_forecast_diagnostics(
    forecast_stats: DailyForecastStats | None,
    *,
    unit: TemperatureUnit,
) -> str | None:
    if forecast_stats is None or forecast_stats.station_latitude is None:
        return None

    def _temp_c(value_c: float | None) -> str:
        if value_c is None:
            return "n/a"
        native = convert_temperature(value_c, TemperatureUnit.CELSIUS, unit)
        return format_temperature(native, unit)

    divergence_display = _delta_display(
        forecast_stats.reference_divergence_c,
        unit,
    )
    providers = forecast_stats.forecast_provider_label or forecast_stats.provider

    return (
        f"Station coords: {forecast_stats.station_latitude:.5f}, "
        f"{forecast_stats.station_longitude:.5f} "
        f"(elev {forecast_stats.station_elevation_m or 0:.0f}m)\n"
        f"Independent sources: {providers}\n"
        f"Reference ({forecast_stats.reference_provider or 'n/a'}): "
        f"{_temp_c(forecast_stats.reference_value_c)}\n"
        f"Max source divergence: {divergence_display}"
    )


def analyze_temperature_event(
    settings: Settings,
    children: list[PolymarketMarket],
) -> MarketAnalysis | None:
    if not children:
        return None

    event_id, event_title, event_description = _event_metadata(children)
    context_market = children[0]
    context_text = build_market_context_text(context_market)
    resolution = parse_resolution_details(context_text)

    event_type = detect_event_temperature_type(event_title, event_description)
    if event_type is None:
        event_type = detect_event_temperature_type(context_text)
    if event_type is None:
        event_type = NormalizedTemperatureType.DAILY_HIGH

    type_valid, type_error = validate_event_type_consistency(
        event_title,
        event_type,
        event_description,
    )

    market_unit, unit_source = detect_market_unit(context_market, context_text=context_text)
    sibling_outcomes = parse_sibling_outcomes(children)
    active_labels = outcome_labels(sibling_outcomes)

    station, station_error = resolve_verified_station_from_text(context_text, settings)
    forecast_stats = None
    if station is not None and station_error is None:
        forecast_stats = fetch_daily_forecast_stats(
            station=station,
            target_date_text=resolution.target_date,
            event_type=event_type,
            settings=settings,
        )
    elif station_error:
        forecast_stats = None

    station_evidence: StationEvidence | None = None
    if station is not None and station.verified and station_error is None:
        try:
            station_evidence = gather_station_evidence(
                settings,
                station,
                target=parse_target_date(resolution.target_date),
                event_type=event_type,
                resolution_url=extract_resolution_url_from_text(context_text)
                or station.resolution_url,
                forecast_stats=forecast_stats,
            )
        except Exception as exc:
            logger.warning(
                "Station evidence gather failed for %s: %s",
                station.code,
                type(exc).__name__,
            )
            station_evidence = None

    forecast_c = forecast_stats.selected_value_c if forecast_stats else None
    forecast_native = (
        convert_temperature(forecast_c, TemperatureUnit.CELSIUS, market_unit)
        if forecast_c is not None
        else None
    )
    sigma_native = _sigma_with_spread(
        default_sigma(market_unit),
        forecast_stats.provider_spread_c if forecast_stats else 0.0,
        market_unit,
    )
    spread_native = _spread_native(
        forecast_stats.provider_spread_c if forecast_stats else 0.0,
        market_unit,
    )
    sibling_probs = (
        distribute_market_probabilities(
            sibling_outcomes,
            DistributionContext(
                consensus_native=forecast_native,
                spread_native=spread_native,
                unit=market_unit,
            ),
            sigma=sigma_native,
        )
        if forecast_native is not None
        else []
    )

    best: tuple[SiblingOutcome, OutcomeProbability, object] | None = None
    for priced in sibling_probs:
        price_valid, _ = validate_actionable_price(priced.outcome.yes_price)
        if not price_valid:
            continue
        if (
            forecast_native is not None
            and not _outcome_near_forecast(
                priced.outcome,
                forecast_native,
                unit=market_unit,
            )
        ):
            continue
        confidence_score = _ensemble_confidence_score(
            calculate_basic_confidence(
                has_station=bool(station and station.verified),
                has_resolution_source=bool(resolution.source),
                has_forecast=forecast_stats is not None,
                has_threshold=True,
            ),
            forecast_stats,
        )
        confidence_level = (
            ConfidenceLevel.HIGH
            if confidence_score >= 0.75
            else ConfidenceLevel.MEDIUM
            if confidence_score >= 0.5
            else ConfidenceLevel.LOW
        )
        result = determine_recommendation_tier(
            priced.probability,
            priced.outcome.yes_price,
            confidence_level,
            market_category=MarketCategory.WEATHER,
        )
        if best is None or priced.probability > best[1].probability:
            best = (priced.outcome, priced, result)

    selected = best[0] if best else None
    selected_prob = best[1] if best else None
    selected_result = best[2] if best else None

    display_pick = pick_display_outcome(sibling_probs)
    display_outcome = selected if selected is not None else (
        display_pick.outcome if display_pick is not None else None
    )
    display_prob = selected_prob if selected_prob is not None else display_pick
    display_price = (
        selected.yes_price
        if selected is not None
        else (display_pick.outcome.yes_price if display_pick is not None else None)
    )

    model_probability = selected_prob.probability if selected_prob else (
        display_prob.probability if display_prob is not None else None
    )
    market_price = display_price
    edge = selected_result.edge if selected_result else None
    ev = selected_result.ev if selected_result else None
    kelly_fraction = selected_result.kelly_fraction if selected_result else None

    confidence_score = _ensemble_confidence_score(
        calculate_basic_confidence(
            has_station=bool(station and station.verified),
            has_resolution_source=bool(resolution.source),
            has_forecast=forecast_stats is not None,
            has_threshold=True,
        ),
        forecast_stats,
    )
    if station_evidence is not None and station_evidence.confidence_delta:
        confidence_score = max(
            0.0,
            min(1.0, confidence_score + station_evidence.confidence_delta),
        )
    confidence = confidence_label(confidence_score)

    mapping_ok, mapping_error = _validate_child_market_mapping(
        selected, selected_prob, selected.yes_price if selected else None
    )
    display_mapping_ok = (
        mapping_ok
        if selected is not None
        else (
            display_outcome is not None
            and display_outcome.label.strip().lower() in active_labels
        )
    )
    distribution_ok, distribution_error = (
        _validate_probability_distribution(
            sibling_probs,
            forecast_native,
            sigma=sigma_native,
            unit=market_unit,
        )
        if forecast_native is not None
        else (True, None)
    )
    range_ok, range_error = (
        _validate_forecast_in_sibling_range(
            forecast_native,
            sibling_outcomes,
            unit=market_unit,
        )
        if forecast_native is not None
        else (True, None)
    )

    valid, skip_reason = _structural_validation_passed(
        event_type=event_type,
        type_valid=type_valid,
        forecast_stats=forecast_stats,
        selected=selected,
        sibling_probs=sibling_probs,
        model_probability=model_probability,
        market_price=market_price,
        edge=edge,
        active_labels=active_labels,
    )
    if station_error and not skip_reason:
        valid = False
        skip_reason = station_error
    if forecast_stats is None and station is not None and station.verified and not skip_reason:
        valid = False
        skip_reason = "FORECAST_UNAVAILABLE"
    if valid and not mapping_ok:
        valid = False
        skip_reason = mapping_error
    if valid and not display_mapping_ok and not skip_reason:
        valid = False
        skip_reason = mapping_error or "OUTCOME_MAPPING_MISMATCH"
    if valid and not distribution_ok:
        valid = False
        skip_reason = distribution_error
    if valid and not range_ok:
        valid = False
        skip_reason = range_error

    if valid and selected_result:
        recommendation_tier = _cap_tier(
            selected_result.tier.value,
            confidence,
            model_probability=model_probability,
            selected_kind=selected.kind.value if selected else None,
            forecast_stats=forecast_stats,
        )
    else:
        recommendation_tier = "SKIP"

    city = extract_city_from_text(context_text, resolution.station_code)
    type_label = normalized_type_label(event_type)

    forecast_display = ""
    expected_low = expected_high = None
    if forecast_native is not None:
        forecast_display = format_temperature(forecast_native, market_unit)
        expected_low, expected_high = expected_range(
            forecast_native,
            sigma_native,
            unit=market_unit,
        )

    forecast_high_display = forecast_display if event_type == NormalizedTemperatureType.DAILY_HIGH else ""
    forecast_low_display = forecast_display if event_type == NormalizedTemperatureType.DAILY_LOW else ""

    reason = skip_reason or (
        f"Model edge {edge * 100:+.1f}% with {confidence} confidence."
        if edge is not None and recommendation_tier not in {"SKIP", "WATCH"}
        else "EV below actionable threshold or validation limits sizing."
    )

    resolution_station = station.code if station and station.verified else resolution.station_code
    provider_spread_display = None
    if forecast_stats and forecast_stats.provider_spread_c > 0:
        spread_native = _spread_native(forecast_stats.provider_spread_c, market_unit)
        provider_spread_display = (
            f"{spread_native:.0f}{unit_symbol(market_unit)}"
        )

    betting_skip_reason = forecast_stats.betting_skip_reason if forecast_stats else None
    forecast_gateway, forecast_models = format_forecast_gateway_models(forecast_stats)
    forecast_confidence = forecast_confidence_level(
        forecast_stats,
        betting_skip_reason=betting_skip_reason,
    )
    display_status = compute_display_status(
        station_verified=bool(station and station.verified),
        station_error=station_error,
        forecast_stats=forecast_stats,
        forecast_native=forecast_native,
        mapping_ok=display_mapping_ok,
        selected_in_event=display_outcome is not None,
        betting_valid=valid,
        recommendation_tier=recommendation_tier,
    )
    display_reason = short_display_reason(
        display_status=display_status,
        validation_error=skip_reason,
        betting_skip_reason=betting_skip_reason,
        station_error=station_error,
        evidence_reason=(
            station_evidence.short_reason if station_evidence is not None else None
        ),
    )
    source_divergence_display = format_source_divergence(
        forecast_stats,
        unit=market_unit,
    )

    def _build_band_rows() -> tuple[tuple[str, float, float, float | None], ...]:
        if not sibling_probs:
            return ()
        ranked = sorted(sibling_probs, key=lambda item: item.probability, reverse=True)
        rows: list[tuple[str, float, float, float | None]] = []
        for item in ranked[:3]:
            market_p = item.outcome.yes_price
            if market_p is None:
                continue
            edge_val = item.probability - market_p
            rows.append((item.outcome.label, item.probability, market_p, edge_val))
        return tuple(rows)

    forecast_sources_line = ""
    if forecast_stats and forecast_stats.provider_names:
        parts = []
        for name in forecast_stats.provider_names:
            parts.append(name)
        forecast_sources_line = ", ".join(parts)

    live_trigger_list: tuple[str, ...] = ()
    if station and station.verified and forecast_native is not None:
        live_trigger_list = tuple(
            build_live_triggers(
                station,
                event_type_label=type_label,
                consensus_native=forecast_native,
                unit=market_unit,
                target_date_local=resolution.target_date,
            )
        )

    return MarketAnalysis(
        market_title=event_title or context_market.question or "Unknown event",
        market_url=market_polymarket_url(context_market),
        city_location=city,
        station_code=resolution_station,
        resolution_source=resolution.source,
        target_date=resolution.target_date,
        market_type=TemperatureMarketType.MAXIMUM_TEMPERATURE if event_type == NormalizedTemperatureType.DAILY_HIGH else TemperatureMarketType.MINIMUM_TEMPERATURE,
        threshold_f=celsius_to_fahrenheit(forecast_c) if forecast_c is not None else None,
        threshold_unit=market_unit.value,
        yes_price=market_price,
        implied_probability=market_price,
        model_probability=model_probability or 0.0,
        edge=edge,
        ev=ev,
        kelly_fraction=kelly_fraction,
        kelly_cap=MAX_BANKROLL_WEATHER,
        confidence=confidence,
        recommendation_tier=recommendation_tier,
        reason=reason or "Analysis unavailable",
        data_sources=[forecast_stats.provider if forecast_stats else "none", "sibling_distribution"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="",
        confidence_score=confidence_score,
        best_outcome=display_outcome.label if display_outcome else "N/A",
        forecast_display=forecast_high_display or forecast_low_display or "N/A",
        liquidity=context_market.liquidity,
        is_priority_us_city=is_priority_us_city(
            city, resolution.station_code, settings.priority_weather_cities
        ),
        event_id=event_id,
        market_id=display_outcome.market_id if display_outcome else context_market.id,
        temperature_type=event_type,
        temperature_type_label=type_label,
        market_unit=market_unit.value,
        provider_unit=TemperatureUnit.CELSIUS.value,
        displayed_unit=market_unit.value,
        conversion_applied=False,
        market_unit_source=unit_source,
        raw_yes_price=market_price,
        price_valid=validate_actionable_price(market_price)[0],
        comparison_text="",
        seasonal_context=None,
        bracket_low=float(expected_low) if expected_low is not None else None,
        bracket_high=float(expected_high) if expected_high is not None else None,
        forecast_value=forecast_c,
        forecast_source=forecast_stats.provider if forecast_stats else None,
        expected_range_display=(
            f"{expected_low}–{expected_high}{unit_symbol(market_unit)}"
            if expected_low is not None
            else None
        ),
        selected_statistic=forecast_stats.selected_statistic if forecast_stats else None,
        validation_error=skip_reason,
        resolution_station=resolution_station,
        forecast_providers=forecast_stats.forecast_provider_label if forecast_stats else None,
        provider_spread_display=provider_spread_display,
        forecast_diagnostics=_format_forecast_diagnostics(
            forecast_stats,
            unit=market_unit,
        ),
        display_status=display_status,
        forecast_confidence=forecast_confidence,
        forecast_gateway=forecast_gateway,
        forecast_models=forecast_models,
        source_divergence_display=source_divergence_display,
        betting_skip_reason=betting_skip_reason,
        station_timezone=(
            forecast_stats.station_timezone
            if forecast_stats
            else (station.timezone if station else None)
        ),
        target_date_local=(
            forecast_stats.target_date_local.isoformat()
            if forecast_stats
            else None
        ),
        display_reason=display_reason,
        band_rows=_build_band_rows(),
        live_triggers=live_trigger_list,
        forecast_sources_line=forecast_sources_line,
        station_bias_f=(
            station_evidence.station_bias_f if station_evidence is not None else None
        ),
        station_bias_days=(
            station_evidence.station_bias_days if station_evidence is not None else 0
        ),
        evidence_short_reason=(
            station_evidence.short_reason if station_evidence is not None else None
        ),
        observed_high_f=(
            station_evidence.wunderground.raw_high_f
            if station_evidence and station_evidence.wunderground
            else None
        ),
        observed_low_f=(
            station_evidence.wunderground.raw_low_f
            if station_evidence and station_evidence.wunderground
            else None
        ),
        observed_rounded_f=(
            (
                station_evidence.wunderground.rounded_high_f
                if event_type == NormalizedTemperatureType.DAILY_HIGH
                else station_evidence.wunderground.rounded_low_f
            )
            if station_evidence and station_evidence.wunderground
            else None
        ),
        wunderground_history_url=(
            station_evidence.wunderground.history_url
            if station_evidence and station_evidence.wunderground
            else None
        ),
        metar_stale=bool(
            station_evidence and station_evidence.metar and station_evidence.metar.stale
        ),
        nws_discussion_office=(
            station_evidence.nws_discussion.office_id
            if station_evidence and station_evidence.nws_discussion
            else None
        ),
        local_tv_accepted=bool(
            station_evidence
            and any(item.accepted for item in station_evidence.local_tv)
        ),
    )
