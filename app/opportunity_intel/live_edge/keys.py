"""Optional Live Edge provider keys from the process environment.

Do not write secrets here. Empty values fail closed in the adapters.
"""

from __future__ import annotations

import os

# PandaScore Bearer token. Covers LoL, CS2, and Dota running matches.
# Free / Fixtures plan: schedules, results, named running matches (1k req/hour).
# In-map gold/kills/rounds are WebSocket frames on paid Real-time Data (Basic),
# not the free REST ``/matches/running`` payload. Missing token fails closed.
PANDASCORE_ENV = "LIVE_ESPORTS_PROVIDER_API_KEY"

# Optional override for Riot LoL Esports persisted-gw ``x-api-key``.
# The public website client key is tried when this is empty; HTTP 403
# is treated as BLOCKED, not as a user secret failure.
LOLESPORTS_ENV = "LIVE_EDGE_LOLESPORTS_API_KEY"


def pandascore_token() -> str:
    return os.getenv(PANDASCORE_ENV, "").strip()


def lolesports_api_key() -> str:
    return os.getenv(LOLESPORTS_ENV, "").strip()
