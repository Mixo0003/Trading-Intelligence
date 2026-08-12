"""Public read-only spot catalog discovery for Market Intelligence venues.

Uses only verified public REST catalog endpoints (official exchange docs) and
the existing ``public_get_json`` helper. Does not authenticate, trade, or
mutate venue state.

V4.5 foundation: perpetual and dated futures remain ``unsupported`` — no
unverified futures catalog endpoints are called and completeness is never
claimed for those product families.

Pagination: all five spot catalog endpoints return a single full payload
(no cursor). Partial results are returned when HTTP/parsing fails closed or
individual rows are malformed — never labeled complete.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable

from app.config.settings import Settings
from app.crypto.api_result import ApiFailure
from app.crypto.discovery_models import (
    CatalogDiscoveryResult,
    DiscoveryCompleteness,
    DiscoveryDiagnostic,
    DiscoveryDiagnosticCode,
    DiscoveryProvenance,
    DiscoveredInstrumentRecord,
    PaginationState,
)
from app.crypto.exchanges.base import public_get_json
from app.crypto.product_types import ProductType, require_product_family
from app.crypto.risk import assert_crypto_read_only
from app.crypto.venue_registry import (
    CanonicalInstrument,
    is_v43_venue,
    require_v43_venue,
)

logger = logging.getLogger(__name__)

# Official public catalog paths (documented; not present in pre-V4.3 adapters).
_BINANCE_EXCHANGE_INFO = "/api/v3/exchangeInfo"
_BINGX_SYMBOLS = "/openApi/spot/v1/common/symbols"
_MEXC_EXCHANGE_INFO = "/api/v3/exchangeInfo"
_OKX_INSTRUMENTS = "/api/v5/public/instruments"
_KUCOIN_SYMBOLS = "/api/v2/symbols"

_SOURCE_BINANCE = "binance_spot_exchange_info"
_SOURCE_BINGX = "bingx_spot_common_symbols"
_SOURCE_MEXC = "mexc_spot_exchange_info"
_SOURCE_OKX = "okx_spot_public_instruments"
_SOURCE_KUCOIN = "kucoin_spot_symbols_v2"


def catalog_discovery_enabled(settings: Settings) -> bool:
    return bool(settings.market_opportunity_catalog_discovery_enabled)


def discover_spot_catalog(
    settings: Settings,
    venue_id: str,
    *,
    now: datetime | None = None,
    session_id: str | None = None,
) -> CatalogDiscoveryResult:
    """Discover the public spot catalog for one V4.3 venue.

    May perform a public HTTP GET when invoked. Callers must not invoke this
    against live venues unless explicitly approved outside Slice 2 Build.
    """
    assert_crypto_read_only(settings)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    clock = clock.astimezone(timezone.utc)
    sid = session_id or f"moi-cat-{uuid.uuid4().hex}"

    if not catalog_discovery_enabled(settings):
        return _terminal(
            session_id=sid,
            venue_id=venue_id,
            product_family=ProductType.SPOT,
            completeness=DiscoveryCompleteness.FAILED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.FEATURE_DISABLED,
                    message="market_opportunity_catalog_discovery_enabled is false",
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id="catalog_discovery_gate",
                endpoint_path="",
                fetched_at=clock,
                request_count=0,
                note="feature flag default off",
            ),
            started_at=clock,
            finished_at=clock,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=False,
                stopped_reason="feature_disabled",
            ),
        )

    if not is_v43_venue(venue_id):
        raise KeyError(f"venue not in V4.3 registry: {venue_id!r}")
    require_v43_venue(venue_id)

    dispatcher: dict[str, Callable[..., CatalogDiscoveryResult]] = {
        "binance": _discover_binance_spot,
        "bingx": _discover_bingx_spot,
        "mexc": _discover_mexc_spot,
        "okx": _discover_okx_spot,
        "kucoin": _discover_kucoin_spot,
    }
    return dispatcher[venue_id](settings, session_id=sid, now=clock)


def discover_catalog(
    settings: Settings,
    venue_id: str,
    product_family: ProductType,
    *,
    now: datetime | None = None,
    session_id: str | None = None,
) -> CatalogDiscoveryResult:
    """Discover catalog for a venue product family (capability-aware).

    Spot uses verified public adapters. Perpetual/dated futures remain
    unsupported when the venue registry declares them unsupported.
    """
    family = require_product_family(product_family)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    clock = clock.astimezone(timezone.utc)
    sid = session_id or f"moi-cat-{uuid.uuid4().hex}"

    if not is_v43_venue(venue_id):
        return _terminal(
            session_id=sid,
            venue_id=venue_id,
            product_family=family,
            completeness=DiscoveryCompleteness.UNSUPPORTED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.UNSUPPORTED_PRODUCT,
                    message=f"venue {venue_id!r} is not in the Market Intelligence registry",
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id="v45_unsupported_venue",
                endpoint_path="",
                fetched_at=clock,
                request_count=0,
            ),
            started_at=clock,
            finished_at=clock,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=True,
                stopped_reason="unsupported_venue",
            ),
        )

    venue = require_v43_venue(venue_id)
    if not venue.supports(family):
        return _terminal(
            session_id=sid,
            venue_id=venue_id,
            product_family=family,
            completeness=DiscoveryCompleteness.UNSUPPORTED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.UNSUPPORTED_PRODUCT,
                    message=(
                        f"{family.value} catalog discovery is unsupported "
                        "(no verified futures adapter in repository; "
                        "V4.5 foundation does not call unverified endpoints)"
                    ),
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id="v45_unsupported_product",
                endpoint_path="",
                fetched_at=clock,
                request_count=0,
            ),
            started_at=clock,
            finished_at=clock,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=True,
                stopped_reason="unsupported_product",
            ),
        )
    if family != ProductType.SPOT:
        # Registry claimed support but no verified adapter exists — fail closed.
        return _terminal(
            session_id=sid,
            venue_id=venue_id,
            product_family=family,
            completeness=DiscoveryCompleteness.FAILED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.UNSUPPORTED_PRODUCT,
                    message=(
                        f"{family.value} marked supported without a verified "
                        "adapter; refusing to invent an HTTP catalog path"
                    ),
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id="v45_missing_adapter",
                endpoint_path="",
                fetched_at=clock,
                request_count=0,
            ),
            started_at=clock,
            finished_at=clock,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=True,
                stopped_reason="missing_verified_adapter",
            ),
        )
    return discover_spot_catalog(
        settings, venue_id, now=clock, session_id=sid
    )


def _terminal(
    *,
    session_id: str,
    venue_id: str,
    product_family: ProductType,
    completeness: DiscoveryCompleteness,
    instruments: tuple[DiscoveredInstrumentRecord, ...],
    diagnostics: tuple[DiscoveryDiagnostic, ...],
    provenance: DiscoveryProvenance,
    started_at: datetime,
    finished_at: datetime,
    pagination: PaginationState,
) -> CatalogDiscoveryResult:
    return CatalogDiscoveryResult(
        session_id=session_id,
        venue_id=venue_id,
        product_family=product_family,
        completeness=completeness,
        instruments=instruments,
        pagination=pagination,
        diagnostics=diagnostics,
        provenance=provenance,
        started_at=started_at,
        finished_at=finished_at,
    )


def _single_page_pagination(
    *,
    exhausted: bool,
    stopped_reason: str | None,
    pages_fetched: int = 1,
) -> PaginationState:
    return PaginationState(
        mode="none",
        pages_fetched=pages_fetched,
        exhausted=exhausted,
        next_cursor=None,
        stopped_reason=stopped_reason,
    )


def _failure_from_api(
    *,
    session_id: str,
    venue_id: str,
    source_id: str,
    endpoint_path: str,
    started_at: datetime,
    finished_at: datetime,
    failure: ApiFailure,
) -> CatalogDiscoveryResult:
    if failure.status_code == 429:
        code = DiscoveryDiagnosticCode.RATE_LIMITED
    elif failure.code in {"http_error"}:
        code = DiscoveryDiagnosticCode.HTTP_ERROR
    elif failure.code == "invalid_json":
        code = DiscoveryDiagnosticCode.INVALID_JSON
    else:
        code = DiscoveryDiagnosticCode.HTTP_STATUS
    return _terminal(
        session_id=session_id,
        venue_id=venue_id,
        product_family=ProductType.SPOT,
        completeness=DiscoveryCompleteness.FAILED,
        instruments=(),
        diagnostics=(
            DiscoveryDiagnostic(code=code, message=failure.message),
        ),
        provenance=DiscoveryProvenance(
            source_id=source_id,
            endpoint_path=endpoint_path,
            fetched_at=finished_at,
            request_count=failure.request_count,
        ),
        started_at=started_at,
        finished_at=finished_at,
        pagination=PaginationState(
            mode="none",
            pages_fetched=0,
            exhausted=False,
            stopped_reason=code.value,
        ),
    )


def _dedupe_append(
    rows: list[DiscoveredInstrumentRecord],
    seen: set[tuple[str, str, str]],
    record: DiscoveredInstrumentRecord,
    diagnostics: list[DiscoveryDiagnostic],
) -> None:
    key = record.instrument.identity_key
    if key in seen:
        diagnostics.append(
            DiscoveryDiagnostic(
                code=DiscoveryDiagnosticCode.DUPLICATE_INSTRUMENT,
                message=f"duplicate identity skipped: {key}",
                venue_market_id=record.instrument.venue_market_id,
            )
        )
        return
    seen.add(key)
    rows.append(record)


def _finalize_rows(
    *,
    session_id: str,
    venue_id: str,
    source_id: str,
    endpoint_path: str,
    started_at: datetime,
    finished_at: datetime,
    request_count: int,
    rows: list[DiscoveredInstrumentRecord],
    diagnostics: list[DiscoveryDiagnostic],
    row_errors: int,
) -> CatalogDiscoveryResult:
    if not rows and row_errors == 0 and not diagnostics:
        completeness = DiscoveryCompleteness.EMPTY
        pagination = _single_page_pagination(exhausted=True, stopped_reason=None)
    elif not rows and row_errors > 0:
        completeness = DiscoveryCompleteness.PARTIAL
        pagination = _single_page_pagination(
            exhausted=False, stopped_reason="malformed_rows"
        )
    elif row_errors > 0:
        completeness = DiscoveryCompleteness.PARTIAL
        pagination = _single_page_pagination(
            exhausted=False, stopped_reason="malformed_rows"
        )
    else:
        completeness = DiscoveryCompleteness.COMPLETE
        pagination = _single_page_pagination(exhausted=True, stopped_reason=None)

    return _terminal(
        session_id=session_id,
        venue_id=venue_id,
        product_family=ProductType.SPOT,
        completeness=completeness,
        instruments=tuple(rows),
        diagnostics=tuple(diagnostics),
        provenance=DiscoveryProvenance(
            source_id=source_id,
            endpoint_path=endpoint_path,
            fetched_at=finished_at,
            request_count=request_count,
        ),
        started_at=started_at,
        finished_at=finished_at,
        pagination=pagination,
    )


def _spot_instrument(
    *,
    venue_id: str,
    venue_market_id: str,
    base: str,
    quote: str,
) -> CanonicalInstrument:
    base_u = base.strip().upper()
    quote_u = quote.strip().upper()
    return CanonicalInstrument(
        venue_id=venue_id,
        product_type=ProductType.SPOT,
        venue_market_id=venue_market_id.strip(),
        base=base_u,
        quote=quote_u,
        normalized_symbol=f"{base_u}/{quote_u}",
        display_name=f"{base_u}/{quote_u}",
    )


def _quality_from_active(active: bool | None) -> str:
    if active is True:
        return "active"
    if active is False:
        return "inactive"
    return "unknown"


# --- Venue discoverers -------------------------------------------------


def _discover_binance_spot(
    settings: Settings, *, session_id: str, now: datetime
) -> CatalogDiscoveryResult:
    path = _BINANCE_EXCHANGE_INFO
    url = f"{settings.crypto_binance_base_url.rstrip('/')}{path}"
    started = now
    # Official docs: permissions=SPOT filters to spot-capable symbols.
    result = public_get_json(settings, url, params={"permissions": "SPOT"})
    finished = datetime.now(timezone.utc)
    if isinstance(result, ApiFailure):
        return _failure_from_api(
            session_id=session_id,
            venue_id="binance",
            source_id=_SOURCE_BINANCE,
            endpoint_path=path,
            started_at=started,
            finished_at=finished,
            failure=result,
        )
    payload = result.data
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        return _terminal(
            session_id=session_id,
            venue_id="binance",
            product_family=ProductType.SPOT,
            completeness=DiscoveryCompleteness.FAILED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.INVALID_PAYLOAD,
                    message="Binance exchangeInfo missing symbols list",
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id=_SOURCE_BINANCE,
                endpoint_path=path,
                fetched_at=finished,
                request_count=result.request_count,
            ),
            started_at=started,
            finished_at=finished,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=False,
                stopped_reason="invalid_payload",
            ),
        )

    rows: list[DiscoveredInstrumentRecord] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    seen: set[tuple[str, str, str]] = set()
    row_errors = 0
    for item in payload["symbols"]:
        if not isinstance(item, dict):
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message="symbol row is not an object",
                )
            )
            continue
        symbol = item.get("symbol")
        base = item.get("baseAsset")
        quote = item.get("quoteAsset")
        status = item.get("status")
        if not isinstance(symbol, str) or not symbol:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing symbol",
                )
            )
            continue
        if not isinstance(base, str) or not base or not isinstance(quote, str) or not quote:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing baseAsset/quoteAsset",
                    venue_market_id=symbol,
                )
            )
            continue
        try:
            instrument = _spot_instrument(
                venue_id="binance",
                venue_market_id=symbol,
                base=base,
                quote=quote,
            )
        except ValueError as exc:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message=str(exc),
                    venue_market_id=symbol,
                )
            )
            continue
        active = isinstance(status, str) and status.upper() == "TRADING"
        record = DiscoveredInstrumentRecord(
            instrument=instrument,
            venue_status=str(status) if status is not None else None,
            quality_status=_quality_from_active(active),
            discovered_at=finished,
            last_seen_at=finished,
            source_id=_SOURCE_BINANCE,
        )
        _dedupe_append(rows, seen, record, diagnostics)

    return _finalize_rows(
        session_id=session_id,
        venue_id="binance",
        source_id=_SOURCE_BINANCE,
        endpoint_path=path,
        started_at=started,
        finished_at=finished,
        request_count=result.request_count,
        rows=rows,
        diagnostics=diagnostics,
        row_errors=row_errors,
    )


def _discover_bingx_spot(
    settings: Settings, *, session_id: str, now: datetime
) -> CatalogDiscoveryResult:
    path = _BINGX_SYMBOLS
    url = f"{settings.crypto_bingx_base_url.rstrip('/')}{path}"
    started = now
    result = public_get_json(settings, url)
    finished = datetime.now(timezone.utc)
    if isinstance(result, ApiFailure):
        return _failure_from_api(
            session_id=session_id,
            venue_id="bingx",
            source_id=_SOURCE_BINGX,
            endpoint_path=path,
            started_at=started,
            finished_at=finished,
            failure=result,
        )
    payload = result.data
    data = payload.get("data") if isinstance(payload, dict) else None
    symbols = data.get("symbols") if isinstance(data, dict) else None
    if not isinstance(symbols, list):
        return _terminal(
            session_id=session_id,
            venue_id="bingx",
            product_family=ProductType.SPOT,
            completeness=DiscoveryCompleteness.FAILED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.INVALID_PAYLOAD,
                    message="BingX symbols payload missing data.symbols list",
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id=_SOURCE_BINGX,
                endpoint_path=path,
                fetched_at=finished,
                request_count=result.request_count,
            ),
            started_at=started,
            finished_at=finished,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=False,
                stopped_reason="invalid_payload",
            ),
        )

    # Official envelope uses code==0 for success when present.
    if isinstance(payload, dict) and "code" in payload and payload.get("code") not in (0, "0"):
        return _terminal(
            session_id=session_id,
            venue_id="bingx",
            product_family=ProductType.SPOT,
            completeness=DiscoveryCompleteness.FAILED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.HTTP_STATUS,
                    message=f"BingX error code={payload.get('code')!r}",
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id=_SOURCE_BINGX,
                endpoint_path=path,
                fetched_at=finished,
                request_count=result.request_count,
            ),
            started_at=started,
            finished_at=finished,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=False,
                stopped_reason="venue_error_code",
            ),
        )

    rows: list[DiscoveredInstrumentRecord] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    seen: set[tuple[str, str, str]] = set()
    row_errors = 0
    for item in symbols:
        if not isinstance(item, dict):
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message="symbol row is not an object",
                )
            )
            continue
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or "-" not in symbol:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing or invalid BingX symbol",
                )
            )
            continue
        base, quote = symbol.split("-", 1)
        if not base or not quote:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message="cannot split BingX symbol into base/quote",
                    venue_market_id=symbol,
                )
            )
            continue
        try:
            instrument = _spot_instrument(
                venue_id="bingx",
                venue_market_id=symbol,
                base=base,
                quote=quote,
            )
        except ValueError as exc:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message=str(exc),
                    venue_market_id=symbol,
                )
            )
            continue
        status = item.get("status")
        active = status in (1, "1")
        inactive = status in (0, "0")
        quality = _quality_from_active(True if active else False if inactive else None)
        record = DiscoveredInstrumentRecord(
            instrument=instrument,
            venue_status=str(status) if status is not None else None,
            quality_status=quality,
            discovered_at=finished,
            last_seen_at=finished,
            source_id=_SOURCE_BINGX,
        )
        _dedupe_append(rows, seen, record, diagnostics)

    return _finalize_rows(
        session_id=session_id,
        venue_id="bingx",
        source_id=_SOURCE_BINGX,
        endpoint_path=path,
        started_at=started,
        finished_at=finished,
        request_count=result.request_count,
        rows=rows,
        diagnostics=diagnostics,
        row_errors=row_errors,
    )


def _discover_mexc_spot(
    settings: Settings, *, session_id: str, now: datetime
) -> CatalogDiscoveryResult:
    path = _MEXC_EXCHANGE_INFO
    url = f"{settings.crypto_mexc_base_url.rstrip('/')}{path}"
    started = now
    result = public_get_json(settings, url)
    finished = datetime.now(timezone.utc)
    if isinstance(result, ApiFailure):
        return _failure_from_api(
            session_id=session_id,
            venue_id="mexc",
            source_id=_SOURCE_MEXC,
            endpoint_path=path,
            started_at=started,
            finished_at=finished,
            failure=result,
        )
    payload = result.data
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        return _terminal(
            session_id=session_id,
            venue_id="mexc",
            product_family=ProductType.SPOT,
            completeness=DiscoveryCompleteness.FAILED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.INVALID_PAYLOAD,
                    message="MEXC exchangeInfo missing symbols list",
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id=_SOURCE_MEXC,
                endpoint_path=path,
                fetched_at=finished,
                request_count=result.request_count,
            ),
            started_at=started,
            finished_at=finished,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=False,
                stopped_reason="invalid_payload",
            ),
        )

    rows: list[DiscoveredInstrumentRecord] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    seen: set[tuple[str, str, str]] = set()
    row_errors = 0
    for item in payload["symbols"]:
        if not isinstance(item, dict):
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message="symbol row is not an object",
                )
            )
            continue
        symbol = item.get("symbol")
        base = item.get("baseAsset")
        quote = item.get("quoteAsset")
        if not isinstance(symbol, str) or not symbol:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing symbol",
                )
            )
            continue
        if not isinstance(base, str) or not base or not isinstance(quote, str) or not quote:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing baseAsset/quoteAsset",
                    venue_market_id=symbol,
                )
            )
            continue
        # Official docs: status 1 online, 2 pause, 3 offline (string or int).
        status = item.get("status")
        spot_allowed = item.get("isSpotTradingAllowed")
        if spot_allowed is False:
            # Not a spot-tradable market in the catalog sense — skip without
            # treating as venue failure.
            continue
        try:
            instrument = _spot_instrument(
                venue_id="mexc",
                venue_market_id=symbol,
                base=base,
                quote=quote,
            )
        except ValueError as exc:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message=str(exc),
                    venue_market_id=symbol,
                )
            )
            continue
        active = status in (1, "1")
        inactive = status in (2, "2", 3, "3")
        quality = _quality_from_active(True if active else False if inactive else None)
        record = DiscoveredInstrumentRecord(
            instrument=instrument,
            venue_status=str(status) if status is not None else None,
            quality_status=quality,
            discovered_at=finished,
            last_seen_at=finished,
            source_id=_SOURCE_MEXC,
        )
        _dedupe_append(rows, seen, record, diagnostics)

    return _finalize_rows(
        session_id=session_id,
        venue_id="mexc",
        source_id=_SOURCE_MEXC,
        endpoint_path=path,
        started_at=started,
        finished_at=finished,
        request_count=result.request_count,
        rows=rows,
        diagnostics=diagnostics,
        row_errors=row_errors,
    )


def _discover_okx_spot(
    settings: Settings, *, session_id: str, now: datetime
) -> CatalogDiscoveryResult:
    path = _OKX_INSTRUMENTS
    url = f"{settings.crypto_okx_base_url.rstrip('/')}{path}"
    started = now
    result = public_get_json(settings, url, params={"instType": "SPOT"})
    finished = datetime.now(timezone.utc)
    if isinstance(result, ApiFailure):
        return _failure_from_api(
            session_id=session_id,
            venue_id="okx",
            source_id=_SOURCE_OKX,
            endpoint_path=path,
            started_at=started,
            finished_at=finished,
            failure=result,
        )
    payload = result.data
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return _terminal(
            session_id=session_id,
            venue_id="okx",
            product_family=ProductType.SPOT,
            completeness=DiscoveryCompleteness.FAILED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.INVALID_PAYLOAD,
                    message="OKX instruments missing data list",
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id=_SOURCE_OKX,
                endpoint_path=path,
                fetched_at=finished,
                request_count=result.request_count,
            ),
            started_at=started,
            finished_at=finished,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=False,
                stopped_reason="invalid_payload",
            ),
        )

    rows: list[DiscoveredInstrumentRecord] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    seen: set[tuple[str, str, str]] = set()
    row_errors = 0
    for item in data:
        if not isinstance(item, dict):
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message="instrument row is not an object",
                )
            )
            continue
        inst_id = item.get("instId")
        base = item.get("baseCcy")
        quote = item.get("quoteCcy")
        state = item.get("state")
        if not isinstance(inst_id, str) or not inst_id:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing instId",
                )
            )
            continue
        if not isinstance(base, str) or not base or not isinstance(quote, str) or not quote:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing baseCcy/quoteCcy",
                    venue_market_id=inst_id,
                )
            )
            continue
        try:
            instrument = _spot_instrument(
                venue_id="okx",
                venue_market_id=inst_id,
                base=base,
                quote=quote,
            )
        except ValueError as exc:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message=str(exc),
                    venue_market_id=inst_id,
                )
            )
            continue
        active = isinstance(state, str) and state.lower() == "live"
        inactive = isinstance(state, str) and state.lower() in {
            "suspend",
            "expired",
            "settlement",
        }
        quality = _quality_from_active(True if active else False if inactive else None)
        record = DiscoveredInstrumentRecord(
            instrument=instrument,
            venue_status=str(state) if state is not None else None,
            quality_status=quality,
            discovered_at=finished,
            last_seen_at=finished,
            source_id=_SOURCE_OKX,
        )
        _dedupe_append(rows, seen, record, diagnostics)

    return _finalize_rows(
        session_id=session_id,
        venue_id="okx",
        source_id=_SOURCE_OKX,
        endpoint_path=path,
        started_at=started,
        finished_at=finished,
        request_count=result.request_count,
        rows=rows,
        diagnostics=diagnostics,
        row_errors=row_errors,
    )


def _discover_kucoin_spot(
    settings: Settings, *, session_id: str, now: datetime
) -> CatalogDiscoveryResult:
    path = _KUCOIN_SYMBOLS
    url = f"{settings.crypto_kucoin_base_url.rstrip('/')}{path}"
    started = now
    result = public_get_json(settings, url)
    finished = datetime.now(timezone.utc)
    if isinstance(result, ApiFailure):
        return _failure_from_api(
            session_id=session_id,
            venue_id="kucoin",
            source_id=_SOURCE_KUCOIN,
            endpoint_path=path,
            started_at=started,
            finished_at=finished,
            failure=result,
        )
    payload = result.data
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return _terminal(
            session_id=session_id,
            venue_id="kucoin",
            product_family=ProductType.SPOT,
            completeness=DiscoveryCompleteness.FAILED,
            instruments=(),
            diagnostics=(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.INVALID_PAYLOAD,
                    message="KuCoin symbols missing data list",
                ),
            ),
            provenance=DiscoveryProvenance(
                source_id=_SOURCE_KUCOIN,
                endpoint_path=path,
                fetched_at=finished,
                request_count=result.request_count,
            ),
            started_at=started,
            finished_at=finished,
            pagination=PaginationState(
                mode="none",
                pages_fetched=0,
                exhausted=False,
                stopped_reason="invalid_payload",
            ),
        )

    rows: list[DiscoveredInstrumentRecord] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    seen: set[tuple[str, str, str]] = set()
    row_errors = 0
    for item in data:
        if not isinstance(item, dict):
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message="symbol row is not an object",
                )
            )
            continue
        symbol = item.get("symbol")
        base = item.get("baseCurrency")
        quote = item.get("quoteCurrency")
        if not isinstance(symbol, str) or not symbol:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing symbol",
                )
            )
            continue
        if not isinstance(base, str) or not base or not isinstance(quote, str) or not quote:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MISSING_REQUIRED_FIELD,
                    message="missing baseCurrency/quoteCurrency",
                    venue_market_id=symbol,
                )
            )
            continue
        try:
            instrument = _spot_instrument(
                venue_id="kucoin",
                venue_market_id=symbol,
                base=base,
                quote=quote,
            )
        except ValueError as exc:
            row_errors += 1
            diagnostics.append(
                DiscoveryDiagnostic(
                    code=DiscoveryDiagnosticCode.MALFORMED_ROW,
                    message=str(exc),
                    venue_market_id=symbol,
                )
            )
            continue
        enable = item.get("enableTrading")
        quality = _quality_from_active(
            True if enable is True else False if enable is False else None
        )
        record = DiscoveredInstrumentRecord(
            instrument=instrument,
            venue_status="enableTrading" if enable is True else str(enable),
            quality_status=quality,
            discovered_at=finished,
            last_seen_at=finished,
            source_id=_SOURCE_KUCOIN,
        )
        _dedupe_append(rows, seen, record, diagnostics)

    return _finalize_rows(
        session_id=session_id,
        venue_id="kucoin",
        source_id=_SOURCE_KUCOIN,
        endpoint_path=path,
        started_at=started,
        finished_at=finished,
        request_count=result.request_count,
        rows=rows,
        diagnostics=diagnostics,
        row_errors=row_errors,
    )
