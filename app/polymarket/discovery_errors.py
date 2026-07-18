"""Weather discovery failure types (no secrets / URLs)."""


class WeatherDiscoveryError(Exception):
    """Raised when every Polymarket discovery path fails with no usable markets."""

    def __init__(self, message: str = "Market discovery is temporarily unavailable.") -> None:
        super().__init__(message)
