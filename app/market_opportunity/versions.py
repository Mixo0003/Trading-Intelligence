"""Version constants for Market Opportunity Intelligence."""

from __future__ import annotations

MARKET_OPPORTUNITY_SCHEMA_VERSION = 1
MARKET_OPPORTUNITY_SCHEMA_MARKER = "market_opportunity_v1"
# Additive V4.3 catalog persistence (sessions + instruments). Does not alter v1.
MARKET_OPPORTUNITY_CATALOG_SCHEMA_MARKER = "market_opportunity_v2_catalog"
MARKET_OPPORTUNITY_CATALOG_SCHEMA_VERSION = 2
SAFETY_POLICY_VERSION = 1
CONFIDENCE_POLICY_VERSION = 1  # ordinal bands only until paper calibration
