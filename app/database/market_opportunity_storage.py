"""Additive Market Opportunity Intelligence storage (market_opportunity_v1)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app.config.settings import Settings
from app.database.storage import get_connection
from app.market_opportunity.models import (
    AssetClass,
    ConfidenceBand,
    EvidenceRef,
    InstrumentRef,
    Opportunity,
    OpportunityFamily,
    OpportunityOutcome,
    OpportunitySide,
    OpportunityStatus,
    PriceLevel,
    Provenance,
)
from app.market_opportunity.versions import (
    MARKET_OPPORTUNITY_CATALOG_SCHEMA_MARKER,
    MARKET_OPPORTUNITY_CATALOG_SCHEMA_VERSION,
    MARKET_OPPORTUNITY_SCHEMA_MARKER,
    MARKET_OPPORTUNITY_SCHEMA_VERSION,
)

MIGRATION_ID = MARKET_OPPORTUNITY_SCHEMA_MARKER
SCHEMA_VERSION = MARKET_OPPORTUNITY_SCHEMA_VERSION
CATALOG_MIGRATION_ID = MARKET_OPPORTUNITY_CATALOG_SCHEMA_MARKER
CATALOG_SCHEMA_VERSION = MARKET_OPPORTUNITY_CATALOG_SCHEMA_VERSION


class MarketOpportunityMigrationError(RuntimeError):
    pass


def is_migration_applied(settings: Settings) -> bool:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'moi_schema_migrations'
            """
        ).fetchone()
        if row is None:
            return False
        found = connection.execute(
            """
            SELECT 1 FROM moi_schema_migrations WHERE migration_id = ?
            """,
            (MIGRATION_ID,),
        ).fetchone()
        return found is not None


def initialize_market_opportunity_schema(settings: Settings) -> None:
    """Idempotent additive apply of market_opportunity_v1 + catalog v2."""
    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _apply_v1_on_connection(connection)
            _apply_catalog_v2_on_connection(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def is_catalog_migration_applied(settings: Settings) -> bool:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'moi_schema_migrations'
            """
        ).fetchone()
        if row is None:
            return False
        found = connection.execute(
            """
            SELECT 1 FROM moi_schema_migrations WHERE migration_id = ?
            """,
            (CATALOG_MIGRATION_ID,),
        ).fetchone()
        return found is not None


def _apply_catalog_v2_on_connection(connection: sqlite3.Connection) -> None:
    """Additive catalog tables. Never DROP or mutate v1 opportunity rows."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS moi_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    existing = connection.execute(
        "SELECT 1 FROM moi_schema_migrations WHERE migration_id = ?",
        (CATALOG_MIGRATION_ID,),
    ).fetchone()
    if existing is not None:
        return

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS moi_catalog_discovery_sessions (
            session_id TEXT PRIMARY KEY,
            venue_id TEXT NOT NULL,
            product_type TEXT NOT NULL,
            completeness TEXT NOT NULL
                CHECK (completeness IN (
                    'complete', 'partial', 'unsupported', 'failed', 'empty'
                )),
            diagnostics_json TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            pagination_json TEXT NOT NULL,
            instrument_count INTEGER NOT NULL CHECK (instrument_count >= 0),
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS moi_catalog_instruments (
            venue_id TEXT NOT NULL,
            product_type TEXT NOT NULL,
            venue_market_id TEXT NOT NULL,
            base_asset TEXT NOT NULL,
            quote_asset TEXT NOT NULL,
            normalized_symbol TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            contract_expiry TEXT,
            venue_status TEXT,
            quality_status TEXT NOT NULL
                CHECK (quality_status IN ('active', 'inactive', 'unknown')),
            source_id TEXT NOT NULL,
            discovery_session_id TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (venue_id, product_type, venue_market_id),
            FOREIGN KEY (discovery_session_id)
                REFERENCES moi_catalog_discovery_sessions (session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_moi_catalog_sessions_venue_finished
            ON moi_catalog_discovery_sessions (venue_id, product_type, finished_at DESC);
        CREATE INDEX IF NOT EXISTS idx_moi_catalog_instruments_symbol
            ON moi_catalog_instruments (normalized_symbol);
        CREATE INDEX IF NOT EXISTS idx_moi_catalog_instruments_last_seen
            ON moi_catalog_instruments (venue_id, product_type, last_seen_at DESC);
        """
    )
    applied_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO moi_schema_migrations (migration_id, schema_version, applied_at)
        VALUES (?, ?, ?)
        """,
        (CATALOG_MIGRATION_ID, CATALOG_SCHEMA_VERSION, applied_at),
    )


def _apply_v1_on_connection(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS moi_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    existing = connection.execute(
        "SELECT 1 FROM moi_schema_migrations WHERE migration_id = ?",
        (MIGRATION_ID,),
    ).fetchone()
    if existing is not None:
        return

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS moi_opportunities (
            opportunity_uid TEXT PRIMARY KEY,
            family TEXT NOT NULL,
            opportunity_type TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT', 'WAIT')),
            status TEXT NOT NULL,
            confidence TEXT NOT NULL CHECK (confidence IN ('LOW', 'MEDIUM', 'HIGH')),
            asset_class TEXT NOT NULL,
            symbol TEXT NOT NULL,
            source_id TEXT NOT NULL,
            venue TEXT NOT NULL,
            market_id TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            verified_price TEXT NOT NULL,
            entry_zone_low TEXT NOT NULL,
            entry_zone_high TEXT NOT NULL,
            targets_json TEXT NOT NULL,
            invalidation_price TEXT NOT NULL,
            time_horizon TEXT NOT NULL,
            reward_risk TEXT NOT NULL,
            counterarguments_json TEXT NOT NULL,
            paper_only INTEGER NOT NULL CHECK (paper_only = 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS moi_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_uid TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            source_event_at TEXT,
            series_kind TEXT NOT NULL,
            is_catalyst INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE (opportunity_uid, evidence_id),
            FOREIGN KEY (opportunity_uid) REFERENCES moi_opportunities (opportunity_uid)
        );

        CREATE TABLE IF NOT EXISTS moi_outcomes (
            opportunity_uid TEXT PRIMARY KEY,
            entry_reached INTEGER,
            entry_missed INTEGER,
            targets_reached_json TEXT,
            invalidation_reached INTEGER,
            mfe TEXT,
            mae TEXT,
            return_1d TEXT,
            return_5d TEXT,
            return_20d TEXT,
            measured_at TEXT,
            FOREIGN KEY (opportunity_uid) REFERENCES moi_opportunities (opportunity_uid)
        );

        CREATE TABLE IF NOT EXISTS moi_runtime_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL,
            detail TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            enabled_detectors_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS idx_moi_opportunities_status_updated
            ON moi_opportunities (status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_moi_evidence_opportunity
            ON moi_evidence (opportunity_uid);
        """
    )
    applied_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO moi_schema_migrations (migration_id, schema_version, applied_at)
        VALUES (?, ?, ?)
        """,
        (MIGRATION_ID, SCHEMA_VERSION, applied_at),
    )


def upsert_runtime_status(
    settings: Settings,
    *,
    status: str,
    detail: str,
    checked_at: datetime,
    enabled_detectors: list[str] | None = None,
) -> None:
    payload = json.dumps(list(enabled_detectors or []))
    checked = checked_at.astimezone(timezone.utc).isoformat()
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO moi_runtime_status (
                id, status, detail, checked_at, enabled_detectors_json
            ) VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                detail = excluded.detail,
                checked_at = excluded.checked_at,
                enabled_detectors_json = excluded.enabled_detectors_json
            """,
            (status, detail, checked, payload),
        )


def get_runtime_status(settings: Settings) -> dict[str, Any] | None:
    if not is_migration_applied(settings):
        return None
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT status, detail, checked_at, enabled_detectors_json
            FROM moi_runtime_status WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return None
    try:
        enabled = json.loads(row[3] or "[]")
    except json.JSONDecodeError:
        enabled = []
    detail_raw = str(row[1] or "")
    try:
        detail_obj: Any = json.loads(detail_raw) if detail_raw.startswith("{") else detail_raw
    except json.JSONDecodeError:
        detail_obj = detail_raw
    return {
        "status": str(row[0]),
        "detail": detail_obj,
        "checked_at": str(row[2]),
        "enabled_detectors": list(enabled),
    }


def list_published_opportunity_uids(settings: Settings) -> list[str]:
    """Returns published opportunity UIDs (newest first)."""
    if not is_migration_applied(settings):
        return []
    with get_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT opportunity_uid FROM moi_opportunities
            WHERE status = 'published'
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [str(row[0]) for row in rows]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _serialize_provenance(provenance: Provenance) -> dict[str, Any]:
    return {
        "source_id": provenance.source_id,
        "fetched_at": _iso(provenance.fetched_at),
        "source_event_at": _iso(provenance.source_event_at),
        "series_kind": provenance.series_kind,
        "note": provenance.note,
    }


def _deserialize_provenance(payload: dict[str, Any]) -> Provenance:
    return Provenance(
        source_id=str(payload["source_id"]),
        fetched_at=_parse_dt(payload.get("fetched_at")) or datetime.now(timezone.utc),
        source_event_at=_parse_dt(payload.get("source_event_at")),
        series_kind=str(payload.get("series_kind") or "eia_daily_spot"),
        note=str(payload.get("note") or ""),
    )


def _serialize_price_level(level: PriceLevel) -> dict[str, Any]:
    return {
        "label": level.label,
        "price": str(level.price),
        "provenance": _serialize_provenance(level.provenance),
    }


def _deserialize_price_level(payload: dict[str, Any] | str) -> PriceLevel:
    if isinstance(payload, str):
        # Legacy bare price string — synthesize minimal provenance.
        return PriceLevel(
            label="price",
            price=Decimal(payload),
            provenance=Provenance(
                source_id="EIA",
                fetched_at=datetime.now(timezone.utc),
                source_event_at=None,
                series_kind="eia_daily_spot",
            ),
        )
    return PriceLevel(
        label=str(payload.get("label") or "price"),
        price=Decimal(str(payload["price"])),
        provenance=_deserialize_provenance(dict(payload.get("provenance") or {})),
    )


def upsert_opportunity(settings: Settings, opportunity: Opportunity) -> None:
    """Persist a Stage B opportunity and replace its evidence rows."""
    if not opportunity.paper_only:
        raise MarketOpportunityMigrationError("refusing non-paper opportunity")
    initialize_market_opportunity_schema(settings)
    verified_json = json.dumps(_serialize_price_level(opportunity.verified_price))
    targets_json = json.dumps(
        [_serialize_price_level(t) for t in opportunity.targets]
    )
    invalidation_json = json.dumps(_serialize_price_level(opportunity.invalidation))
    counterarguments_json = json.dumps(list(opportunity.counterarguments))
    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT INTO moi_opportunities (
                    opportunity_uid, family, opportunity_type, side, status,
                    confidence, asset_class, symbol, source_id, venue, market_id,
                    display_name, verified_price, entry_zone_low, entry_zone_high,
                    targets_json, invalidation_price, time_horizon, reward_risk,
                    counterarguments_json, paper_only, created_at, updated_at,
                    expires_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?
                )
                ON CONFLICT(opportunity_uid) DO UPDATE SET
                    family = excluded.family,
                    opportunity_type = excluded.opportunity_type,
                    side = excluded.side,
                    status = excluded.status,
                    confidence = excluded.confidence,
                    asset_class = excluded.asset_class,
                    symbol = excluded.symbol,
                    source_id = excluded.source_id,
                    venue = excluded.venue,
                    market_id = excluded.market_id,
                    display_name = excluded.display_name,
                    verified_price = excluded.verified_price,
                    entry_zone_low = excluded.entry_zone_low,
                    entry_zone_high = excluded.entry_zone_high,
                    targets_json = excluded.targets_json,
                    invalidation_price = excluded.invalidation_price,
                    time_horizon = excluded.time_horizon,
                    reward_risk = excluded.reward_risk,
                    counterarguments_json = excluded.counterarguments_json,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    opportunity.opportunity_uid,
                    opportunity.family.value,
                    opportunity.opportunity_type,
                    opportunity.side.value,
                    opportunity.status.value,
                    opportunity.confidence.value,
                    opportunity.instrument.asset_class.value,
                    opportunity.instrument.symbol,
                    opportunity.instrument.source_id,
                    opportunity.instrument.venue,
                    opportunity.instrument.market_id,
                    opportunity.instrument.display_name,
                    verified_json,
                    str(opportunity.entry_zone_low),
                    str(opportunity.entry_zone_high),
                    targets_json,
                    invalidation_json,
                    opportunity.time_horizon,
                    str(opportunity.reward_risk),
                    counterarguments_json,
                    _iso(opportunity.created_at),
                    _iso(opportunity.updated_at),
                    _iso(opportunity.expires_at),
                ),
            )
            connection.execute(
                "DELETE FROM moi_evidence WHERE opportunity_uid = ?",
                (opportunity.opportunity_uid,),
            )
            for evidence in opportunity.evidence:
                connection.execute(
                    """
                    INSERT INTO moi_evidence (
                        opportunity_uid, evidence_id, summary, source_id,
                        fetched_at, source_event_at, series_kind, is_catalyst, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        opportunity.opportunity_uid,
                        evidence.evidence_id,
                        evidence.summary,
                        evidence.provenance.source_id,
                        _iso(evidence.provenance.fetched_at),
                        _iso(evidence.provenance.source_event_at),
                        evidence.provenance.series_kind,
                        1 if evidence.is_catalyst else 0,
                        evidence.provenance.note,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _row_to_dict(
    row: sqlite3.Row | tuple[Any, ...],
    columns: tuple[str, ...],
) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        data = {key: row[key] for key in row.keys()}
    else:
        data = {columns[i]: row[i] for i in range(len(columns))}
    return data


_OPP_COLUMNS = (
    "opportunity_uid",
    "family",
    "opportunity_type",
    "side",
    "status",
    "confidence",
    "asset_class",
    "symbol",
    "source_id",
    "venue",
    "market_id",
    "display_name",
    "verified_price",
    "entry_zone_low",
    "entry_zone_high",
    "targets_json",
    "invalidation_price",
    "time_horizon",
    "reward_risk",
    "counterarguments_json",
    "paper_only",
    "created_at",
    "updated_at",
    "expires_at",
)


def _load_evidence(
    connection: sqlite3.Connection, opportunity_uid: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT evidence_id, summary, source_id, fetched_at, source_event_at,
               series_kind, is_catalyst, note
        FROM moi_evidence
        WHERE opportunity_uid = ?
        ORDER BY id ASC
        """,
        (opportunity_uid,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "evidence_id": str(row[0]),
                "summary": str(row[1]),
                "source_id": str(row[2]),
                "fetched_at": row[3],
                "source_event_at": row[4],
                "series_kind": str(row[5]),
                "is_catalyst": bool(row[6]),
                "note": str(row[7] or ""),
            }
        )
    return out


def _opportunity_dict_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row | tuple[Any, ...],
) -> dict[str, Any]:
    data = _row_to_dict(row, _OPP_COLUMNS)
    uid = str(data["opportunity_uid"])
    try:
        verified = json.loads(str(data["verified_price"]))
    except json.JSONDecodeError:
        verified = {"label": "verified", "price": str(data["verified_price"])}
    try:
        targets = json.loads(str(data["targets_json"]))
    except json.JSONDecodeError:
        targets = []
    try:
        invalidation = json.loads(str(data["invalidation_price"]))
    except json.JSONDecodeError:
        invalidation = {
            "label": "invalidation",
            "price": str(data["invalidation_price"]),
        }
    try:
        counterarguments = json.loads(str(data["counterarguments_json"] or "[]"))
    except json.JSONDecodeError:
        counterarguments = []

    # Signal period from verified provenance source_event_at when present.
    signal_period: str | None = None
    if isinstance(verified, dict):
        prov = verified.get("provenance") or {}
        event_at = prov.get("source_event_at")
        if event_at:
            signal_period = str(event_at)[:10]

    evidence = _load_evidence(connection, uid)
    outcome_row = connection.execute(
        """
        SELECT entry_reached, entry_missed, targets_reached_json,
               invalidation_reached, mfe, mae, return_1d, return_5d,
               return_20d, measured_at
        FROM moi_outcomes WHERE opportunity_uid = ?
        """,
        (uid,),
    ).fetchone()
    outcome: dict[str, Any] | None = None
    if outcome_row is not None:
        try:
            targets_reached = json.loads(outcome_row[2] or "[]")
        except json.JSONDecodeError:
            targets_reached = []
        outcome = {
            "opportunity_uid": uid,
            "entry_reached": None if outcome_row[0] is None else bool(outcome_row[0]),
            "entry_missed": None if outcome_row[1] is None else bool(outcome_row[1]),
            "targets_reached": targets_reached,
            "invalidation_reached": (
                None if outcome_row[3] is None else bool(outcome_row[3])
            ),
            "mfe": outcome_row[4],
            "mae": outcome_row[5],
            "return_1d": outcome_row[6],
            "return_5d": outcome_row[7],
            "return_20d": outcome_row[8],
            "measured_at": outcome_row[9],
        }

    return {
        "opportunity_uid": uid,
        "family": data["family"],
        "opportunity_type": data["opportunity_type"],
        "side": data["side"],
        "status": data["status"],
        "confidence": data["confidence"],
        "asset_class": data["asset_class"],
        "symbol": data["symbol"],
        "source_id": data["source_id"],
        "venue": data["venue"],
        "market_id": data["market_id"],
        "display_name": data["display_name"],
        "verified_price": verified,
        "entry_zone_low": data["entry_zone_low"],
        "entry_zone_high": data["entry_zone_high"],
        "targets": targets,
        "invalidation": invalidation,
        "invalidation_price": (
            invalidation.get("price") if isinstance(invalidation, dict) else invalidation
        ),
        "time_horizon": data["time_horizon"],
        "reward_risk": data["reward_risk"],
        "counterarguments": counterarguments,
        "paper_only": bool(data["paper_only"]),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "expires_at": data["expires_at"],
        "signal_period": signal_period,
        "evidence": evidence,
        "outcome": outcome,
    }


def get_opportunity(
    settings: Settings,
    opportunity_uid: str,
    *,
    as_model: bool = False,
) -> dict[str, Any] | Opportunity | None:
    """Return opportunity dict (default) or reconstructed Opportunity model."""
    if not is_migration_applied(settings):
        return None
    with get_connection(settings) as connection:
        row = connection.execute(
            f"""
            SELECT {", ".join(_OPP_COLUMNS)}
            FROM moi_opportunities
            WHERE opportunity_uid = ?
            """,
            (opportunity_uid,),
        ).fetchone()
        if row is None:
            return None
        payload = _opportunity_dict_from_row(connection, row)
    if as_model:
        return reconstruct_opportunity(payload)
    return payload


def list_opportunities(
    settings: Settings,
    *,
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not is_migration_applied(settings):
        return []
    limit = max(1, min(int(limit), 500))
    clauses: list[str] = []
    params: list[Any] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_connection(settings) as connection:
        rows = connection.execute(
            f"""
            SELECT {", ".join(_OPP_COLUMNS)}
            FROM moi_opportunities
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_opportunity_dict_from_row(connection, row) for row in rows]


def reconstruct_opportunity(payload: dict[str, Any]) -> Opportunity:
    """Rebuild an Opportunity dataclass from a storage dict."""
    verified = _deserialize_price_level(payload["verified_price"])
    targets_raw = payload.get("targets") or []
    targets = tuple(_deserialize_price_level(t) for t in targets_raw)
    invalidation_raw = payload.get("invalidation") or {
        "label": "invalidation",
        "price": payload.get("invalidation_price"),
    }
    invalidation = _deserialize_price_level(invalidation_raw)
    evidence_items: list[EvidenceRef] = []
    for item in payload.get("evidence") or []:
        evidence_items.append(
            EvidenceRef(
                evidence_id=str(item["evidence_id"]),
                summary=str(item["summary"]),
                provenance=Provenance(
                    source_id=str(item["source_id"]),
                    fetched_at=_parse_dt(item.get("fetched_at"))
                    or datetime.now(timezone.utc),
                    source_event_at=_parse_dt(item.get("source_event_at")),
                    series_kind=str(item.get("series_kind") or "eia_daily_spot"),
                    note=str(item.get("note") or ""),
                ),
                is_catalyst=bool(item.get("is_catalyst")),
            )
        )
    outcome = None
    raw_outcome = payload.get("outcome")
    if isinstance(raw_outcome, dict) and raw_outcome.get("opportunity_uid"):
        targets_reached = tuple(
            bool(x) for x in (raw_outcome.get("targets_reached") or []) if x is not None
        )
        outcome = OpportunityOutcome(
            opportunity_uid=str(raw_outcome["opportunity_uid"]),
            entry_reached=raw_outcome.get("entry_reached"),
            entry_missed=raw_outcome.get("entry_missed"),
            targets_reached=targets_reached,
            invalidation_reached=raw_outcome.get("invalidation_reached"),
            mfe=Decimal(str(raw_outcome["mfe"])) if raw_outcome.get("mfe") is not None else None,
            mae=Decimal(str(raw_outcome["mae"])) if raw_outcome.get("mae") is not None else None,
            return_1d=(
                Decimal(str(raw_outcome["return_1d"]))
                if raw_outcome.get("return_1d") is not None
                else None
            ),
            return_5d=(
                Decimal(str(raw_outcome["return_5d"]))
                if raw_outcome.get("return_5d") is not None
                else None
            ),
            return_20d=(
                Decimal(str(raw_outcome["return_20d"]))
                if raw_outcome.get("return_20d") is not None
                else None
            ),
            measured_at=_parse_dt(raw_outcome.get("measured_at")),
        )
    return Opportunity(
        opportunity_uid=str(payload["opportunity_uid"]),
        family=OpportunityFamily(str(payload["family"])),
        opportunity_type=str(payload["opportunity_type"]),
        side=OpportunitySide(str(payload["side"])),
        instrument=InstrumentRef(
            asset_class=AssetClass(str(payload["asset_class"])),
            symbol=str(payload["symbol"]),
            source_id=str(payload["source_id"]),
            venue=str(payload["venue"]),
            market_id=str(payload["market_id"]),
            display_name=str(payload.get("display_name") or ""),
        ),
        status=OpportunityStatus(str(payload["status"])),
        confidence=ConfidenceBand(str(payload["confidence"])),
        verified_price=verified,
        entry_zone_low=Decimal(str(payload["entry_zone_low"])),
        entry_zone_high=Decimal(str(payload["entry_zone_high"])),
        targets=targets,
        invalidation=invalidation,
        time_horizon=str(payload["time_horizon"]),
        reward_risk=Decimal(str(payload["reward_risk"])),
        evidence=tuple(evidence_items),
        counterarguments=tuple(str(x) for x in (payload.get("counterarguments") or [])),
        paper_only=True,
        created_at=_parse_dt(payload.get("created_at")) or datetime.now(timezone.utc),
        updated_at=_parse_dt(payload.get("updated_at")) or datetime.now(timezone.utc),
        expires_at=_parse_dt(payload.get("expires_at")),
        outcome=outcome,
    )


def upsert_outcome(settings: Settings, outcome: OpportunityOutcome) -> None:
    initialize_market_opportunity_schema(settings)
    targets_json = json.dumps(
        [None if x is None else bool(x) for x in outcome.targets_reached]
    )
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO moi_outcomes (
                opportunity_uid, entry_reached, entry_missed, targets_reached_json,
                invalidation_reached, mfe, mae, return_1d, return_5d, return_20d,
                measured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_uid) DO UPDATE SET
                entry_reached = excluded.entry_reached,
                entry_missed = excluded.entry_missed,
                targets_reached_json = excluded.targets_reached_json,
                invalidation_reached = excluded.invalidation_reached,
                mfe = excluded.mfe,
                mae = excluded.mae,
                return_1d = excluded.return_1d,
                return_5d = excluded.return_5d,
                return_20d = excluded.return_20d,
                measured_at = excluded.measured_at
            """,
            (
                outcome.opportunity_uid,
                None if outcome.entry_reached is None else int(bool(outcome.entry_reached)),
                None if outcome.entry_missed is None else int(bool(outcome.entry_missed)),
                targets_json,
                (
                    None
                    if outcome.invalidation_reached is None
                    else int(bool(outcome.invalidation_reached))
                ),
                None if outcome.mfe is None else str(outcome.mfe),
                None if outcome.mae is None else str(outcome.mae),
                None if outcome.return_1d is None else str(outcome.return_1d),
                None if outcome.return_5d is None else str(outcome.return_5d),
                None if outcome.return_20d is None else str(outcome.return_20d),
                _iso(outcome.measured_at),
            ),
        )


def list_open_for_measurement(settings: Settings) -> list[dict[str, Any]]:
    """Published/expired/invalidated opportunities not yet calibrated/measured-complete."""
    if not is_migration_applied(settings):
        return []
    with get_connection(settings) as connection:
        rows = connection.execute(
            f"""
            SELECT {", ".join("o." + c for c in _OPP_COLUMNS)}
            FROM moi_opportunities o
            LEFT JOIN moi_outcomes m ON m.opportunity_uid = o.opportunity_uid
            WHERE o.status IN ('published', 'expired', 'invalidated')
              AND (
                    m.opportunity_uid IS NULL
                 OR m.return_5d IS NULL
                 OR o.status IN ('expired', 'invalidated')
              )
              AND o.status != 'calibrated'
            ORDER BY o.updated_at ASC
            """
        ).fetchall()
        return [_opportunity_dict_from_row(connection, row) for row in rows]


def update_opportunity_status(
    settings: Settings,
    opportunity_uid: str,
    status: str,
) -> None:
    if not is_migration_applied(settings):
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(settings) as connection:
        connection.execute(
            """
            UPDATE moi_opportunities
            SET status = ?, updated_at = ?
            WHERE opportunity_uid = ?
            """,
            (status, now, opportunity_uid),
        )


def signal_period_from_uid(opportunity_uid: str) -> date | None:
    """Parse ``wti:<detector>:<YYYY-MM-DD>`` UIDs."""
    parts = str(opportunity_uid).split(":")
    if len(parts) >= 3:
        try:
            return date.fromisoformat(parts[-1][:10])
        except ValueError:
            return None
    return None


def persist_catalog_discovery_result(
    settings: Settings,
    result: Any,
) -> None:
    """Persist a CatalogDiscoveryResult additively.

    Upserts instruments by identity key and updates ``last_seen_at``.
    Never deletes or mass-deactivates instruments missing from a partial result.
    After a COMPLETE catalog only, instruments absent from that result for the
    same venue×product are marked ``inactive`` (complete-miss rule).
    """
    from app.crypto.discovery_models import CatalogDiscoveryResult

    if not isinstance(result, CatalogDiscoveryResult):
        raise TypeError("result must be CatalogDiscoveryResult")
    if not is_migration_applied(settings):
        initialize_market_opportunity_schema(settings)
    if not is_catalog_migration_applied(settings):
        initialize_market_opportunity_schema(settings)

    diagnostics_json = json.dumps(
        [
            {
                "code": d.code.value,
                "message": d.message,
                "venue_market_id": d.venue_market_id,
            }
            for d in result.diagnostics
        ]
    )
    provenance_json = json.dumps(
        {
            "source_id": result.provenance.source_id,
            "endpoint_path": result.provenance.endpoint_path,
            "fetched_at": result.provenance.fetched_at.astimezone(timezone.utc).isoformat(),
            "request_count": result.provenance.request_count,
            "note": result.provenance.note,
        }
    )
    pagination_json = json.dumps(
        {
            "mode": result.pagination.mode,
            "pages_fetched": result.pagination.pages_fetched,
            "exhausted": result.pagination.exhausted,
            "next_cursor": result.pagination.next_cursor,
            "stopped_reason": result.pagination.stopped_reason,
        }
    )
    created_at = datetime.now(timezone.utc).isoformat()
    started_at = result.started_at.astimezone(timezone.utc).isoformat()
    finished_at = result.finished_at.astimezone(timezone.utc).isoformat()

    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT INTO moi_catalog_discovery_sessions (
                    session_id, venue_id, product_type, completeness,
                    diagnostics_json, provenance_json, pagination_json,
                    instrument_count, started_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.session_id,
                    result.venue_id,
                    result.product_family.value,
                    result.completeness.value,
                    diagnostics_json,
                    provenance_json,
                    pagination_json,
                    len(result.instruments),
                    started_at,
                    finished_at,
                    created_at,
                ),
            )
            for row in result.instruments:
                inst = row.instrument
                discovered = row.discovered_at.astimezone(timezone.utc).isoformat()
                last_seen = row.last_seen_at.astimezone(timezone.utc).isoformat()
                connection.execute(
                    """
                    INSERT INTO moi_catalog_instruments (
                        venue_id, product_type, venue_market_id,
                        base_asset, quote_asset, normalized_symbol, display_name,
                        contract_expiry, venue_status, quality_status, source_id,
                        discovery_session_id, discovered_at, last_seen_at,
                        diagnostics_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(venue_id, product_type, venue_market_id) DO UPDATE SET
                        base_asset = excluded.base_asset,
                        quote_asset = excluded.quote_asset,
                        normalized_symbol = excluded.normalized_symbol,
                        display_name = excluded.display_name,
                        contract_expiry = excluded.contract_expiry,
                        venue_status = excluded.venue_status,
                        quality_status = excluded.quality_status,
                        source_id = excluded.source_id,
                        discovery_session_id = excluded.discovery_session_id,
                        last_seen_at = excluded.last_seen_at,
                        diagnostics_json = excluded.diagnostics_json
                    """,
                    (
                        inst.venue_id,
                        inst.product_type.value,
                        inst.venue_market_id,
                        inst.base,
                        inst.quote,
                        inst.normalized_symbol,
                        inst.display_name,
                        inst.contract_expiry,
                        row.venue_status,
                        row.quality_status,
                        row.source_id,
                        result.session_id,
                        discovered,
                        last_seen,
                        "",
                    ),
                )
            # Complete-miss rule: only after a COMPLETE catalog may absent
            # instruments for this venue×product be marked inactive.
            # Partial/failed/empty never delist (additive upsert only).
            if result.completeness.value == "complete":
                present_ids = {
                    str(row.instrument.venue_market_id) for row in result.instruments
                }
                _deactivate_absent_on_connection(
                    connection,
                    venue_id=result.venue_id,
                    product_type=result.product_family.value,
                    present_venue_market_ids=present_ids,
                    discovery_session_id=result.session_id,
                    seen_at=finished_at,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _deactivate_absent_on_connection(
    connection: sqlite3.Connection,
    *,
    venue_id: str,
    product_type: str,
    present_venue_market_ids: set[str],
    discovery_session_id: str,
    seen_at: str,
) -> int:
    """Mark active rows inactive when missing from a COMPLETE catalog result."""
    rows = connection.execute(
        """
        SELECT venue_market_id FROM moi_catalog_instruments
        WHERE venue_id = ? AND product_type = ? AND quality_status = 'active'
        """,
        (venue_id, product_type),
    ).fetchall()
    deactivated = 0
    for row in rows:
        market_id = str(row[0])
        if market_id in present_venue_market_ids:
            continue
        connection.execute(
            """
            UPDATE moi_catalog_instruments
            SET quality_status = 'inactive',
                discovery_session_id = ?,
                last_seen_at = ?,
                diagnostics_json = ?
            WHERE venue_id = ? AND product_type = ? AND venue_market_id = ?
              AND quality_status = 'active'
            """,
            (
                discovery_session_id,
                seen_at,
                "complete_miss:absent_from_complete_catalog",
                venue_id,
                product_type,
                market_id,
            ),
        )
        deactivated += 1
    return deactivated


def list_catalog_instruments(
    settings: Settings,
    *,
    venue_id: str | None = None,
    product_type: str | None = None,
    quality_status: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read persisted catalog instruments (no delist side effects)."""
    if not is_catalog_migration_applied(settings):
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if venue_id:
        clauses.append("venue_id = ?")
        params.append(venue_id)
    if product_type:
        clauses.append("product_type = ?")
        params.append(product_type)
    if quality_status:
        clauses.append("quality_status = ?")
        params.append(quality_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    bound = max(1, min(int(limit), 5000))
    params.append(bound)
    with get_connection(settings) as connection:
        rows = connection.execute(
            f"""
            SELECT venue_id, product_type, venue_market_id, base_asset, quote_asset,
                   normalized_symbol, display_name, contract_expiry, venue_status,
                   quality_status, source_id, discovery_session_id,
                   discovered_at, last_seen_at
            FROM moi_catalog_instruments
            {where}
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

