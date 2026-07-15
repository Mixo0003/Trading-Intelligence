from typing import Any

import requests

from app.config.settings import Settings
from app.metrics.scan_metrics import active_scan_metrics


class PolymarketClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gamma_base_url = settings.polymarket_gamma_base_url.rstrip("/")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        metrics = active_scan_metrics()
        if metrics is not None:
            metrics.polymarket_requests += 1

        url = f"{self.gamma_base_url}{path}"
        headers = {
            "User-Agent": "Trading Intelligence Bot",
            "Accept": "application/json",
        }

        response = requests.get(
            url,
            params=params or {},
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
