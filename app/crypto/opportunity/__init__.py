"""V4.3 Crypto Opportunity Scanner package."""

from __future__ import annotations

from app.crypto.opportunity.catalog import (
    ensure_default_profile,
    load_top_crypto_opportunities,
)
from app.crypto.opportunity.formatting import (
    format_crypto_opportunity_card,
    format_opportunity_history,
    format_top_crypto_opportunities,
)
from app.crypto.opportunity.models import PaperNetOpportunity, ScanReport
from app.crypto.opportunity.scanner import scan_crypto_opportunities
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    balanced_crypto_settings,
)

__all__ = [
    "CryptoOpportunitySettings",
    "PaperNetOpportunity",
    "ScanReport",
    "balanced_crypto_settings",
    "ensure_default_profile",
    "format_crypto_opportunity_card",
    "format_opportunity_history",
    "format_top_crypto_opportunities",
    "load_top_crypto_opportunities",
    "scan_crypto_opportunities",
]
