"""In-process and SQLite cache for completed weather scan snapshots."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config.settings import Settings
from app.database.storage import get_connection
from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType
from app.region.types import ScanRegion, parse_scan_region
from app.signals.forecast_display import (
    DISPLAY_VALIDATED,
    DISPLAY_WATCH,
    has_forecast_result,
)
from app.signals.market_analysis import MarketAnalysis, ScanProgress

logger = logging.getLogger(__name__)

SCAN_SNAPSHOTS_KEY = "latest_scan_snapshots"
SNAPSHOT_KEY_PREFIX = "latest_scan"


def snapshot_cache_key(region: ScanRegion) -> str:
    return f"{SNAPSHOT_KEY_PREFIX}:{region.value}"


@dataclass(frozen=True)
class CachedScanSnapshot:
    completed_at: str
    progress: ScanProgress
    region: ScanRegion = ScanRegion.ALL

    @property
    def analyzed_count(self) -> int:
        analyses = self.progress.all_analyses or self.progress.results
        return len(analyses)

    @property
    def actionable_analyses(self) -> list[MarketAnalysis]:
        analyses = self.progress.all_analyses or self.progress.results
        return [item for item in analyses if item.display_status == DISPLAY_VALIDATED]

    @property
    def watchlist_count(self) -> int:
        analyses = self.progress.all_analyses or self.progress.results
        return sum(
            1 for item in analyses
            if item.display_status in {DISPLAY_VALIDATED, DISPLAY_WATCH}
        )

    @property
    def forecast_result_count(self) -> int:
        analyses = self.progress.all_analyses or self.progress.results
        return sum(
            1 for item in analyses
            if has_forecast_result(item.display_status, item.forecast_display)
        )

    @property
    def validated_count(self) -> int:
        return len(self.actionable_analyses)

    @property
    def skipped_validation_count(self) -> int:
        analyses = self.progress.all_analyses or self.progress.results
        return sum(
            1 for item in analyses
            if item.display_status not in {DISPLAY_VALIDATED, DISPLAY_WATCH}
            and has_forecast_result(item.display_status, item.forecast_display)
        )


def _enum_value(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    return value


def _analysis_to_dict(analysis: MarketAnalysis) -> dict[str, Any]:
    return {
        "market_title": analysis.market_title,
        "market_url": analysis.market_url,
        "city_location": analysis.city_location,
        "station_code": analysis.station_code,
        "resolution_source": analysis.resolution_source,
        "target_date": analysis.target_date,
        "market_type": _enum_value(analysis.market_type),
        "threshold_f": analysis.threshold_f,
        "threshold_unit": analysis.threshold_unit,
        "yes_price": analysis.yes_price,
        "implied_probability": analysis.implied_probability,
        "model_probability": analysis.model_probability,
        "edge": analysis.edge,
        "ev": analysis.ev,
        "kelly_fraction": analysis.kelly_fraction,
        "kelly_cap": analysis.kelly_cap,
        "confidence": analysis.confidence,
        "recommendation_tier": analysis.recommendation_tier,
        "reason": analysis.reason,
        "data_sources": analysis.data_sources,
        "analyzed_at": analysis.analyzed_at,
        "message": analysis.message,
        "confidence_score": analysis.confidence_score,
        "best_outcome": analysis.best_outcome,
        "forecast_display": analysis.forecast_display,
        "liquidity": analysis.liquidity,
        "is_priority_us_city": analysis.is_priority_us_city,
        "event_id": analysis.event_id,
        "market_id": analysis.market_id,
        "temperature_type": _enum_value(analysis.temperature_type),
        "temperature_type_label": analysis.temperature_type_label,
        "market_unit": analysis.market_unit,
        "provider_unit": analysis.provider_unit,
        "displayed_unit": analysis.displayed_unit,
        "conversion_applied": analysis.conversion_applied,
        "market_unit_source": analysis.market_unit_source,
        "raw_yes_price": analysis.raw_yes_price,
        "price_valid": analysis.price_valid,
        "comparison_text": analysis.comparison_text,
        "seasonal_context": analysis.seasonal_context,
        "bracket_low": analysis.bracket_low,
        "bracket_high": analysis.bracket_high,
        "forecast_value": analysis.forecast_value,
        "forecast_source": analysis.forecast_source,
        "expected_range_display": analysis.expected_range_display,
        "selected_statistic": analysis.selected_statistic,
        "validation_error": analysis.validation_error,
        "resolution_station": analysis.resolution_station,
        "forecast_providers": analysis.forecast_providers,
        "provider_spread_display": analysis.provider_spread_display,
        "forecast_diagnostics": analysis.forecast_diagnostics,
        "display_status": analysis.display_status,
        "forecast_confidence": analysis.forecast_confidence,
        "forecast_gateway": analysis.forecast_gateway,
        "forecast_models": analysis.forecast_models,
        "source_divergence_display": analysis.source_divergence_display,
        "betting_skip_reason": analysis.betting_skip_reason,
        "station_timezone": analysis.station_timezone,
        "target_date_local": analysis.target_date_local,
        "display_reason": analysis.display_reason,
    }


def _analysis_from_dict(data: dict[str, Any]) -> MarketAnalysis:
    return MarketAnalysis(
        market_title=data["market_title"],
        market_url=data["market_url"],
        city_location=data["city_location"],
        station_code=data.get("station_code"),
        resolution_source=data.get("resolution_source"),
        target_date=data.get("target_date"),
        market_type=TemperatureMarketType(data["market_type"]),
        threshold_f=data.get("threshold_f"),
        threshold_unit=data["threshold_unit"],
        yes_price=data.get("yes_price"),
        implied_probability=data.get("implied_probability"),
        model_probability=data["model_probability"],
        edge=data.get("edge"),
        ev=data.get("ev"),
        kelly_fraction=data.get("kelly_fraction"),
        kelly_cap=data["kelly_cap"],
        confidence=data["confidence"],
        recommendation_tier=data["recommendation_tier"],
        reason=data["reason"],
        data_sources=data["data_sources"],
        analyzed_at=data["analyzed_at"],
        message=data["message"],
        confidence_score=data.get("confidence_score", 0.0),
        best_outcome=data.get("best_outcome", "N/A"),
        forecast_display=data.get("forecast_display", ""),
        liquidity=data.get("liquidity"),
        is_priority_us_city=data.get("is_priority_us_city", False),
        event_id=data.get("event_id"),
        market_id=data.get("market_id"),
        temperature_type=NormalizedTemperatureType(
            data.get("temperature_type", NormalizedTemperatureType.EXACT_TEMPERATURE.value)
        ),
        temperature_type_label=data.get("temperature_type_label", ""),
        market_unit=data.get("market_unit", "FAHRENHEIT"),
        provider_unit=data.get("provider_unit", "FAHRENHEIT"),
        displayed_unit=data.get("displayed_unit", "FAHRENHEIT"),
        conversion_applied=data.get("conversion_applied", False),
        market_unit_source=data.get("market_unit_source", ""),
        raw_yes_price=data.get("raw_yes_price"),
        price_valid=data.get("price_valid", False),
        comparison_text=data.get("comparison_text", ""),
        seasonal_context=data.get("seasonal_context"),
        bracket_low=data.get("bracket_low"),
        bracket_high=data.get("bracket_high"),
        forecast_value=data.get("forecast_value"),
        forecast_source=data.get("forecast_source"),
        expected_range_display=data.get("expected_range_display"),
        selected_statistic=data.get("selected_statistic"),
        validation_error=data.get("validation_error"),
        resolution_station=data.get("resolution_station"),
        forecast_providers=data.get("forecast_providers"),
        provider_spread_display=data.get("provider_spread_display"),
        forecast_diagnostics=data.get("forecast_diagnostics"),
        display_status=data.get("display_status", "SKIP — INSUFFICIENT DATA"),
        forecast_confidence=data.get("forecast_confidence", "LOW"),
        forecast_gateway=data.get("forecast_gateway"),
        forecast_models=data.get("forecast_models"),
        source_divergence_display=data.get("source_divergence_display"),
        betting_skip_reason=data.get("betting_skip_reason"),
        station_timezone=data.get("station_timezone"),
        target_date_local=data.get("target_date_local"),
        display_reason=data.get("display_reason", ""),
    )


def _progress_to_dict(progress: ScanProgress) -> dict[str, Any]:
    return {
        "discovered": progress.discovered,
        "accepted": progress.accepted,
        "skipped": progress.skipped,
        "skip_reasons": progress.skip_reasons,
        "results": [_analysis_to_dict(item) for item in progress.results],
        "all_analyses": [_analysis_to_dict(item) for item in progress.all_analyses],
        "worldwide_discovered": progress.worldwide_discovered,
        "direct_temperature_markets_discovered": (
            progress.direct_temperature_markets_discovered
        ),
        "priority_us_discovered": progress.priority_us_discovered,
        "regional_count": progress.regional_count,
        "parent_events_discovered": progress.parent_events_discovered,
        "child_markets_discovered": progress.child_markets_discovered,
        "parent_events_in_region": progress.parent_events_in_region,
        "child_markets_loaded_for_analysis": progress.child_markets_loaded_for_analysis,
        "parent_events_analyzed": progress.parent_events_analyzed,
        "scan_region": progress.scan_region,
        "scan_duration_seconds": progress.scan_duration_seconds,
    }


def _progress_from_dict(data: dict[str, Any]) -> ScanProgress:
    return ScanProgress(
        discovered=data.get("discovered", 0),
        accepted=data.get("accepted", 0),
        skipped=data.get("skipped", 0),
        skip_reasons=data.get("skip_reasons", {}),
        results=[_analysis_from_dict(item) for item in data.get("results", [])],
        all_analyses=[_analysis_from_dict(item) for item in data.get("all_analyses", [])],
        worldwide_discovered=data.get("worldwide_discovered", 0),
        direct_temperature_markets_discovered=data.get(
            "direct_temperature_markets_discovered",
            data.get("worldwide_discovered", 0),
        ),
        priority_us_discovered=data.get("priority_us_discovered", 0),
        regional_count=data.get("regional_count", 0),
        parent_events_discovered=data.get("parent_events_discovered", 0),
        child_markets_discovered=data.get("child_markets_discovered", 0),
        parent_events_in_region=data.get("parent_events_in_region", 0),
        child_markets_loaded_for_analysis=data.get(
            "child_markets_loaded_for_analysis", 0
        ),
        parent_events_analyzed=data.get("parent_events_analyzed", 0),
        scan_region=data.get("scan_region", ScanRegion.ALL.value),
        scan_duration_seconds=data.get("scan_duration_seconds", 0.0),
        metrics=None,
    )


def _snapshot_to_json(snapshot: CachedScanSnapshot) -> str:
    payload = {
        "completed_at": snapshot.completed_at,
        "region": snapshot.region.value,
        "progress": _progress_to_dict(snapshot.progress),
    }
    return json.dumps(payload)


def _snapshot_from_json(raw: str) -> CachedScanSnapshot:
    data = json.loads(raw)
    region = parse_scan_region(data.get("region"), ScanRegion.ALL)
    return CachedScanSnapshot(
        completed_at=data["completed_at"],
        progress=_progress_from_dict(data["progress"]),
        region=region,
    )


def _user_cache(bot_data: dict | None, telegram_id: int) -> dict[str, CachedScanSnapshot]:
    root = bot_data.setdefault(SCAN_SNAPSHOTS_KEY, {}) if bot_data is not None else {}
    return root.setdefault(telegram_id, {})


def _ensure_scan_snapshots_table(settings: Settings) -> None:
    with get_connection(settings) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS scan_snapshots (
                telegram_id INTEGER NOT NULL,
                region TEXT NOT NULL DEFAULT 'ALL',
                completed_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (telegram_id, region)
            )
        """)
        rows = connection.execute("PRAGMA table_info(scan_snapshots)").fetchall()
        columns = {row[1] for row in rows}
        if "region" not in columns:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS scan_snapshots_v2 (
                    telegram_id INTEGER NOT NULL,
                    region TEXT NOT NULL DEFAULT 'ALL',
                    completed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, region)
                )
            """)
            connection.execute("""
                INSERT OR REPLACE INTO scan_snapshots_v2 (telegram_id, region, completed_at, payload)
                SELECT telegram_id, 'ALL', completed_at, payload FROM scan_snapshots
            """)
            connection.execute("DROP TABLE scan_snapshots")
            connection.execute("ALTER TABLE scan_snapshots_v2 RENAME TO scan_snapshots")


def save_scan_snapshot(
    settings: Settings,
    telegram_id: int,
    progress: ScanProgress,
    bot_data: dict | None = None,
    region: ScanRegion | None = None,
) -> CachedScanSnapshot:
    scan_region = region or parse_scan_region(progress.scan_region, ScanRegion.ALL)
    snapshot = CachedScanSnapshot(
        completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        progress=progress,
        region=scan_region,
    )
    if bot_data is not None:
        cache = _user_cache(bot_data, telegram_id)
        cache[snapshot_cache_key(scan_region)] = snapshot

    try:
        _ensure_scan_snapshots_table(settings)
        with get_connection(settings) as connection:
            connection.execute("""
                INSERT INTO scan_snapshots (telegram_id, region, completed_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id, region) DO UPDATE SET
                    completed_at = excluded.completed_at,
                    payload = excluded.payload
            """, (
                telegram_id,
                scan_region.value,
                snapshot.completed_at,
                _snapshot_to_json(snapshot),
            ))
    except Exception:
        logger.exception(
            "Failed to persist scan snapshot for user %s region %s",
            telegram_id,
            scan_region.value,
        )

    return snapshot


def load_latest_scan_snapshot(
    settings: Settings,
    telegram_id: int,
    bot_data: dict | None = None,
    region: ScanRegion = ScanRegion.ALL,
) -> CachedScanSnapshot | None:
    cache_key = snapshot_cache_key(region)
    if bot_data is not None:
        cache = bot_data.get(SCAN_SNAPSHOTS_KEY, {}).get(telegram_id, {})
        snapshot = cache.get(cache_key)
        if snapshot is not None:
            return snapshot

    try:
        _ensure_scan_snapshots_table(settings)
        with get_connection(settings) as connection:
            row = connection.execute("""
                SELECT completed_at, payload
                FROM scan_snapshots
                WHERE telegram_id = ? AND region = ?
                LIMIT 1
            """, (telegram_id, region.value)).fetchone()
        if row is None:
            return None
        snapshot = _snapshot_from_json(row["payload"])
        if bot_data is not None:
            _user_cache(bot_data, telegram_id)[cache_key] = snapshot
        return snapshot
    except Exception:
        logger.exception(
            "Failed to load scan snapshot for user %s region %s",
            telegram_id,
            region.value,
        )
        return None
