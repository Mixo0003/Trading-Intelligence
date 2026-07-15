from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class ScanMetrics:
    polymarket_requests: int = 0
    weather_requests: int = 0
    reference_requests: int = 0
    anthropic_requests: int = 0
    markets_discovered: int = 0
    worldwide_discovered: int = 0  # Legacy direct-market endpoint count.
    direct_temperature_markets_discovered: int = 0
    priority_us_discovered: int = 0
    parent_events_discovered: int = 0
    child_markets_discovered: int = 0
    parent_events_in_region: int = 0
    child_markets_loaded_for_analysis: int = 0
    parent_events_analyzed: int = 0
    markets_accepted: int = 0
    markets_analyzed: int = 0
    markets_skipped: int = 0
    markets_excluded_before_weather: int = 0
    opportunities_returned: int = 0
    scan_region: str = "ALL"
    scan_duration_seconds: float = 0.0


_active_metrics: ContextVar[ScanMetrics | None] = ContextVar("scan_metrics", default=None)


def active_scan_metrics() -> ScanMetrics | None:
    return _active_metrics.get()


def reset_scan_metrics() -> ScanMetrics:
    metrics = ScanMetrics()
    _active_metrics.set(metrics)
    return metrics


@contextmanager
def scan_metrics_context():
    metrics = reset_scan_metrics()
    try:
        yield metrics
    finally:
        _active_metrics.set(None)
