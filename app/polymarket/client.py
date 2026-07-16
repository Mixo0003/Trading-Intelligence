from typing import Any

from app.config.settings import Settings
from app.http.retry import get_with_retry
from app.metrics.scan_metrics import active_scan_metrics
from app.signals.scan_runtime import active_scan_state, note_polymarket_429


class PolymarketClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gamma_base_url = settings.polymarket_gamma_base_url.rstrip("/")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        metrics = active_scan_metrics()
        if metrics is not None:
            metrics.polymarket_requests += 1

        state = active_scan_state()
        if state is not None and path.startswith("/events"):
            slug = (params or {}).get("slug")
            event_id = (params or {}).get("id")
            if slug and slug in state.event_by_slug_cache:
                return state.event_by_slug_cache[slug]
            if event_id is not None and str(event_id) in state.event_by_id_cache:
                return state.event_by_id_cache[str(event_id)]

        url = f"{self.gamma_base_url}{path}"
        headers = {
            "User-Agent": "Trading Intelligence Bot",
            "Accept": "application/json",
        }

        response = get_with_retry(
            url,
            params=params or {},
            headers=headers,
            timeout=20,
        )
        if response.status_code == 429:
            note_polymarket_429()
        response.raise_for_status()
        data = response.json()

        if state is not None and path.startswith("/events"):
            slug = (params or {}).get("slug")
            if slug:
                state.event_by_slug_cache[str(slug)] = data
            if isinstance(data, list) and data and isinstance(data[0], dict):
                eid = data[0].get("id")
                if eid is not None:
                    state.event_by_id_cache[str(eid)] = data
            elif isinstance(data, dict) and data.get("id") is not None:
                state.event_by_id_cache[str(data["id"])] = data

        return data
