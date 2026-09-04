"""Production composite live-state provider. Missing feeds fail closed."""

from __future__ import annotations

from datetime import datetime

from app.opportunity_intel.live_edge.feeds.binance_spot import BinanceSpotProvider
from app.opportunity_intel.live_edge.feeds.bo3 import Bo3Cs2Provider
from app.opportunity_intel.live_edge.feeds.leaguepedia_lol import LeaguepediaLolProvider
from app.opportunity_intel.live_edge.feeds.liquipedia import LiquipediaMatchProvider
from app.opportunity_intel.live_edge.feeds.lolesports import LoLEsportsProvider
from app.opportunity_intel.live_edge.feeds.mlb import MlbStatsProvider
from app.opportunity_intel.live_edge.feeds.opendota import OpenDotaLiveProvider
from app.opportunity_intel.live_edge.feeds.openliga import OpenLigaDbProvider
from app.opportunity_intel.live_edge.feeds.pandascore import PandaScoreProvider
from app.opportunity_intel.live_edge.feeds.sportsdb import TheSportsDbProvider
from app.opportunity_intel.live_edge.feeds.weather_bridge import WeatherIntelligenceBridge
from app.opportunity_intel.live_edge.models import (
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)
from app.opportunity_intel.live_edge.providers import (
    CompositeLiveStateProvider,
    LiveStateProvider,
    OrderedFallbackProvider,
)


def build_production_provider() -> LiveStateProvider:
    pandascore = PandaScoreProvider()
    liquipedia = LiquipediaMatchProvider()
    return CompositeLiveStateProvider(
        (
            OrderedFallbackProvider(
                (LeaguepediaLolProvider(), LoLEsportsProvider(), pandascore, liquipedia),
                categories=(LiveEdgeCategory.ESPORTS_LOL,),
            ),
            OrderedFallbackProvider(
                (Bo3Cs2Provider(), pandascore, liquipedia),
                categories=(LiveEdgeCategory.ESPORTS_CS2,),
            ),
            OrderedFallbackProvider(
                (OpenDotaLiveProvider(), pandascore, liquipedia),
                categories=(LiveEdgeCategory.ESPORTS_DOTA,),
            ),
            MlbStatsProvider(),
            TheSportsDbProvider(),
            OpenLigaDbProvider(),
            BinanceSpotProvider(),
            WeatherIntelligenceBridge(),
        )
    )


class RecordingProvider:
    """Test helper: wrap a provider and record last snapshots."""

    def __init__(self, inner: LiveStateProvider) -> None:
        self.inner = inner
        self.last: tuple[LiveStateSnapshot, ...] = ()

    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        self.last = self.inner.snapshots_for(market, identity, now=now)
        return self.last
