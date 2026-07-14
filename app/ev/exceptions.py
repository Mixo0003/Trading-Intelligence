class InvalidProbabilityError(ValueError):
    """Raised when a probability is outside [0, 1]."""


class InvalidMarketPriceError(ValueError):
    """Raised when a market price is outside [0, 1]."""


class InvalidDecimalOddsError(ValueError):
    """Raised when decimal odds are not greater than 1."""


class InvalidKellyFractionError(ValueError):
    """Raised when a Kelly fraction is outside [0, 1]."""
