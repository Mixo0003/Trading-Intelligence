from typing import Any

import requests

from app.config.settings import Settings


class PolymarketClient:
    def __init__(self, settings: Settings, timeout: float = 10.0) -> None:
        self.settings = settings
        self.gamma_base_url = settings.polymarket_gamma_base_url.rstrip("/")
        self.timeout = timeout

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.gamma_base_url}{path}"
        headers = {
            "User-Agent": "Trading Intelligence Bot",
            "Accept": "application/json",
        }

        response = requests.get(
            url,
            params=params or {},
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()
