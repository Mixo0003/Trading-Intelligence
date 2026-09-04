"""V2.4.4 live-feed adapters, fallback chain, health, and discovery pagination."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.opportunity_intel.live_edge.classify import classify_market
from app.opportunity_intel.live_edge.decisions import decide
from app.opportunity_intel.live_edge.discovery import discover_active_markets
from app.opportunity_intel.live_edge.economics import executable_quote_from_book
from app.opportunity_intel.live_edge.evaluators import evaluate_market
from app.opportunity_intel.live_edge.feeds.bo3 import Bo3Cs2Provider
from app.opportunity_intel.live_edge.feeds.leaguepedia_lol import LeaguepediaLolProvider
from app.opportunity_intel.live_edge.feeds.lolesports import LoLEsportsProvider
from app.opportunity_intel.live_edge.feeds.opendota import OpenDotaLiveProvider
from app.opportunity_intel.live_edge.feeds.pandascore import PandaScoreProvider
from app.opportunity_intel.live_edge.fees import fetch_taker_fee_usd
from app.opportunity_intel.live_edge.health import (
    ProviderHealthStatus,
    clear_provider_health,
    get_provider_health,
)
from app.opportunity_intel.live_edge.http_json import (
    clear_live_edge_http_cache,
    fetch_json,
    fetch_json_result,
)
from app.opportunity_intel.live_edge.matching import match_event, match_map_market
from app.opportunity_intel.live_edge.models import (
    EvidenceTier,
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveEdgeDecision,
    MatchStatus,
    SkipReason,
)
from app.opportunity_intel.live_edge.probability import map_win_probability
from app.opportunity_intel.live_edge.providers import (
    FixtureLiveStateProvider,
    OrderedFallbackProvider,
)
from tests.opportunity_intel.test_v244_live_edge import (
    NOW,
    _book,
    _gamma_event,
    _lol_state,
    _market,
)


@pytest.fixture(autouse=True)
def _reset():
    clear_live_edge_http_cache()
    clear_provider_health()
    yield
    clear_live_edge_http_cache()
    clear_provider_health()


def _lol_identity():
    return classify_market(
        _market(
            question="KT Rolster vs DRX - Map 1 Winner",
            event_title="KT Rolster Challengers vs Kiwoom DRX Challengers",
            tags=("lol", "esports"),
        )
    )


def _cs_identity():
    return classify_market(
        _market(
            question="Natus Vincere vs G2 - Map 1 Winner",
            event_title="Natus Vincere vs G2",
            tags=("cs2", "counter-strike"),
        )
    )


def _dota_identity():
    return classify_market(
        _market(
            question="Team Secret vs OG - Series Winner",
            event_title="Team Secret vs OG",
            tags=("dota 2",),
        )
    )


def test_leaguepedia_named_live_and_skips_finished():
    start_live = (NOW - timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
    start_done = (NOW - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "cargoquery": [
            {
                "title": {
                    "Team1": "KT Rolster Challengers",
                    "Team2": "Kiwoom DRX Challengers",
                    "Team1Score": "",
                    "Team2Score": "",
                    "BestOf": "5",
                    "DateTime UTC": start_live,
                    "Winner": "",
                    "OverviewPage": "LCK CL/2026 Season/Season Playoffs",
                }
            },
            {
                "title": {
                    "Team1": "Cloud9",
                    "Team2": "Shopify Rebellion",
                    "Team1Score": "2",
                    "Team2Score": "1",
                    "BestOf": "3",
                    "DateTime UTC": start_done,
                    "Winner": "1",
                    "OverviewPage": "LCS/2026 Season/Summer Season",
                }
            },
            {
                "title": {
                    "Team1": "LYON (2024 American Team)",
                    "Team2": "Team Liquid",
                    "Team1Score": "1",
                    "Team2Score": "0",
                    "BestOf": "3",
                    "DateTime UTC": start_live,
                    "Winner": "",
                    "OverviewPage": "LCS/2026 Season/Summer Season",
                }
            },
        ]
    }
    with patch(
        "app.opportunity_intel.live_edge.feeds.leaguepedia_lol.fetch_json",
        return_value=payload,
    ):
        snaps = LeaguepediaLolProvider().snapshots_for(_market(question="x"), _lol_identity(), now=NOW)
    names = {item.participants for item in snaps}
    assert ("KT Rolster Challengers", "Kiwoom DRX Challengers") in names
    assert ("Cloud9", "Shopify Rebellion") not in names
    lyon = next(item for item in snaps if item.participants[0].startswith("LYON"))
    assert "LYON" in lyon.aliases
    assert lyon.maps_won == (1, 0)
    assert lyon.current_map == 2
    kt = next(item for item in snaps if item.participants[0].startswith("KT"))
    assert kt.status == "live"
    assert kt.current_map == 1


def test_leaguepedia_malformed_and_ratelimit_fail_closed():
    identity = _lol_identity()
    with patch(
        "app.opportunity_intel.live_edge.feeds.leaguepedia_lol.fetch_json",
        return_value={"error": {"code": "ratelimited", "info": "wait"}},
    ):
        assert LeaguepediaLolProvider().snapshots_for(_market(question="x"), identity, now=NOW) == ()
    health = get_provider_health("leaguepedia-lol")
    assert health is not None
    assert health.status == ProviderHealthStatus.RATE_LIMITED
    with patch(
        "app.opportunity_intel.live_edge.feeds.leaguepedia_lol.fetch_json",
        return_value={"cargoquery": "bad"},
    ):
        assert LeaguepediaLolProvider().snapshots_for(_market(question="x"), identity, now=NOW) == ()


def test_bo3_current_named_cs2_and_empty():
    identity = _cs_identity()
    payload = {
        "results": [
            {
                "id": 99,
                "status": "current",
                "team1_score": 1,
                "team2_score": 0,
                "bo_type": 3,
                "start_date": NOW.isoformat(),
                "teams": [
                    {"name": "Natus Vincere", "slug": "natus-vincere"},
                    {"name": "G2", "slug": "g2"},
                ],
                "tournament": {"name": "ESL Pro League"},
                "games": [
                    {"state": "done", "map_name": "de_inferno"},
                    {"state": "live", "map_name": "de_mirage", "team1_score": 10, "team2_score": 4},
                ],
            }
        ]
    }
    with patch(
        "app.opportunity_intel.live_edge.feeds.bo3.fetch_json",
        side_effect=[payload, {"team1_score": 10, "team2_score": 4}],
    ):
        snaps = Bo3Cs2Provider().snapshots_for(_market(question="x"), identity, now=NOW)
    assert len(snaps) == 1
    assert snaps[0].participants == ("Natus Vincere", "G2")
    assert snaps[0].maps_won == (1, 0)
    assert snaps[0].current_map == 2
    assert snaps[0].round_score == (10, 4)
    with patch("app.opportunity_intel.live_edge.feeds.bo3.fetch_json", return_value={"results": []}):
        assert Bo3Cs2Provider().snapshots_for(_market(question="x"), identity, now=NOW) == ()


def test_pandascore_missing_key_fail_closed(monkeypatch):
    monkeypatch.delenv("LIVE_ESPORTS_PROVIDER_API_KEY", raising=False)
    snaps = PandaScoreProvider().snapshots_for(_market(question="x"), _lol_identity(), now=NOW)
    assert snaps == ()
    health = get_provider_health("pandascore")
    assert health is not None
    assert health.status == ProviderHealthStatus.AUTH_REQUIRED


def test_pandascore_running_parse(monkeypatch):
    monkeypatch.setenv("LIVE_ESPORTS_PROVIDER_API_KEY", "test-token")
    payload = [
        {
            "id": 7,
            "begin_at": NOW.isoformat(),
            "number_of_games": 3,
            "live": {"supported": False},
            "opponents": [
                {"opponent": {"name": "T1"}},
                {"opponent": {"name": "Gen.G"}},
            ],
            "results": [{"score": 1}, {"score": 0}],
            "tournament": {"name": "LCK"},
            "games": [{"status": "running", "position": 2}],
        }
    ]
    with patch(
        "app.opportunity_intel.live_edge.feeds.pandascore.fetch_json", return_value=payload
    ):
        snaps = PandaScoreProvider().snapshots_for(_market(question="x"), _lol_identity(), now=NOW)
    assert len(snaps) == 1
    assert snaps[0].participants == ("T1", "Gen.G")
    assert snaps[0].maps_won == (1, 0)
    assert snaps[0].current_map == 2
    assert snaps[0].kills is None
    assert snaps[0].gold_difference is None
    assert snaps[0].extra.get("in_map") is False
    assert snaps[0].extra.get("live_supported") is False


def test_pandascore_does_not_invent_in_map_from_series_only(monkeypatch):
    monkeypatch.setenv("LIVE_ESPORTS_PROVIDER_API_KEY", "test-token")
    payload = [
        {
            "id": 8,
            "begin_at": NOW.isoformat(),
            "modified_at": NOW.isoformat(),
            "number_of_games": 5,
            "detailed_stats": False,
            "live": {"supported": False, "url": None},
            "opponents": [
                {"opponent": {"name": "Natus Vincere", "acronym": "NAVI"}},
                {"opponent": {"name": "G2", "acronym": "G2"}},
            ],
            "results": [{"score": 0}, {"score": 0}],
            "tournament": {"name": "ESL Pro League"},
            "games": [{"status": "running", "position": 1, "length": 840}],
        }
    ]
    with patch(
        "app.opportunity_intel.live_edge.feeds.pandascore.fetch_json", return_value=payload
    ):
        snaps = PandaScoreProvider().snapshots_for(_market(question="x"), _cs_identity(), now=NOW)
    assert len(snaps) == 1
    assert snaps[0].aliases == ("NAVI",)
    assert snaps[0].game_time_seconds == 840
    assert snaps[0].round_score is None
    assert snaps[0].kills is None
    cs_market = _market(
        question="Will Natus Vincere win Map 1?",
        event_title="Natus Vincere vs G2",
        tags=("cs2",),
    )
    evaluation = evaluate_market(
        cs_market,
        classify_market(cs_market),
        FixtureLiveStateProvider(snaps),
        now=NOW,
    )
    assert evaluation.skip_reason is None
    assert evaluation.probability is None
    assert evaluation.live_state is not None
    assert evaluation.match.status == MatchStatus.VERIFIED_MATCH
    watched = decide(evaluation, None, now=NOW)
    assert watched.decision == LiveEdgeDecision.WATCH
    assert watched.decision != LiveEdgeDecision.BUY_NOW


def test_pandascore_uses_structured_in_map_when_present(monkeypatch):
    monkeypatch.setenv("LIVE_ESPORTS_PROVIDER_API_KEY", "test-token")
    payload = [
        {
            "id": 9,
            "begin_at": NOW.isoformat(),
            "number_of_games": 3,
            "opponents": [
                {"opponent": {"name": "T1"}},
                {"opponent": {"name": "Gen.G"}},
            ],
            "results": [{"score": 1}, {"score": 0}],
            "tournament": {"name": "LCK"},
            "games": [
                {
                    "status": "running",
                    "position": 2,
                    "blue": {"kills": 14, "gold": 42000, "towers": 8},
                    "red": {"kills": 3, "gold": 25000, "towers": 2},
                }
            ],
        }
    ]
    with patch(
        "app.opportunity_intel.live_edge.feeds.pandascore.fetch_json", return_value=payload
    ):
        snaps = PandaScoreProvider().snapshots_for(_market(question="x"), _lol_identity(), now=NOW)
    assert snaps[0].kills == (14, 3)
    assert snaps[0].gold_difference == 17000
    assert snaps[0].towers == (8, 2)
    assert snaps[0].extra.get("in_map") is True


def test_fallback_prefers_stronger_in_map_without_conflict():
    series_only = _lol_state(kills=None, gold_difference=None, towers=None, dragons=None, barons=None)
    richer = _lol_state(
        kills=(14, 2),
        gold_difference=12000,
        towers=(8, 1),
        provenance=LiveDataProvenance(
            provider="pandascore",
            event_identifier="ps-1",
            source_locator="https://api.pandascore.co/lol/matches/running",
            fetched_at=NOW,
            source_timestamp=NOW,
            reliability="structured_keyed",
        ),
    )
    chain = OrderedFallbackProvider(
        (FixtureLiveStateProvider((series_only,)), FixtureLiveStateProvider((richer,))),
        categories=(LiveEdgeCategory.ESPORTS_LOL,),
    )
    snaps = chain.snapshots_for(_market(question="Team A vs Team B"), _lol_identity(), now=NOW)
    assert snaps[0].provenance is not None
    assert snaps[0].provenance.provider == "pandascore"
    assert snaps[0].kills == (14, 2)
    assert snaps[0].extra.get("source_conflict") is None


def test_lolesports_403_and_window_parse():
    identity = _lol_identity()
    with patch(
        "app.opportunity_intel.live_edge.feeds.lolesports.fetch_json", return_value=None
    ):
        assert LoLEsportsProvider().snapshots_for(_market(question="x"), identity, now=NOW) == ()
    payload = {
        "data": {
            "schedule": {
                "events": [
                    {
                        "id": "evt-1",
                        "league": {"name": "LCK"},
                        "match": {
                            "teams": [
                                {"name": "T1", "result": {"gameWins": 1}},
                                {"name": "Gen.G", "result": {"gameWins": 0}},
                            ],
                            "games": [
                                {"id": "g1", "state": "completed"},
                                {"id": "g2", "state": "inProgress"},
                            ],
                        },
                    }
                ]
            }
        }
    }
    window = {
        "frames": [
            {
                "gameTime": 1600000,
                "blueTeam": {
                    "totalKills": 12,
                    "totalGold": 40000,
                    "towers": 7,
                    "dragons": ["infernal"],
                    "barons": 1,
                    "inhibitors": 1,
                },
                "redTeam": {
                    "totalKills": 3,
                    "totalGold": 28000,
                    "towers": 2,
                    "dragons": [],
                    "barons": 0,
                    "inhibitors": 0,
                },
            }
        ]
    }
    with patch(
        "app.opportunity_intel.live_edge.feeds.lolesports.fetch_json",
        side_effect=[payload, window],
    ):
        snaps = LoLEsportsProvider().snapshots_for(_market(question="x"), identity, now=NOW)
    assert len(snaps) == 1
    assert snaps[0].kills == (12, 3)
    assert snaps[0].gold_difference == 12000
    assert snaps[0].current_map == 2


def test_http_403_429_timeout_malformed():
    response_403 = MagicMock(status_code=403, text='{"message":"Forbidden"}')
    with patch("app.opportunity_intel.live_edge.http_json.get_with_retry", return_value=response_403):
        result = fetch_json_result("https://example.test/forbidden", provider="lolesports")
    assert result.payload is None
    assert result.status_code == 403
    assert get_provider_health("lolesports").status == ProviderHealthStatus.BLOCKED

    response_429 = MagicMock(status_code=429, text="rate")
    with patch("app.opportunity_intel.live_edge.http_json.get_with_retry", return_value=response_429):
        result = fetch_json_result("https://example.test/limited", provider="bo3-cs2", cache_key="r429")
    assert result.status_code == 429
    assert get_provider_health("bo3-cs2").status == ProviderHealthStatus.RATE_LIMITED

    with patch(
        "app.opportunity_intel.live_edge.http_json.get_with_retry",
        side_effect=TimeoutError("timeout"),
    ):
        result = fetch_json_result("https://example.test/timeout", provider="opendota-live", cache_key="t1")
    assert result.payload is None
    assert get_provider_health("opendota-live").status == ProviderHealthStatus.OFFLINE

    bad = MagicMock(status_code=200)
    bad.json.side_effect = ValueError("no json")
    with patch("app.opportunity_intel.live_edge.http_json.get_with_retry", return_value=bad):
        assert fetch_json("https://example.test/bad", cache_key="bad") is None


def test_fallback_uses_second_when_first_empty():
    empty = FixtureLiveStateProvider(())
    second = FixtureLiveStateProvider((_lol_state(),))
    chain = OrderedFallbackProvider((empty, second), categories=(LiveEdgeCategory.ESPORTS_LOL,))
    snaps = chain.snapshots_for(_market(question="Team A vs Team B"), _lol_identity(), now=NOW)
    assert len(snaps) == 1
    assert snaps[0].extra.get("source_conflict") is None


def test_source_conflict_blocks_buy():
    a = _lol_state(maps_won=(2, 0), current_map=3)
    b = _lol_state(
        maps_won=(1, 1),
        current_map=3,
        provenance=LiveDataProvenance(
            provider="other",
            event_identifier="other",
            source_locator="fixture://other",
            fetched_at=NOW,
            source_timestamp=NOW,
            reliability="verified_fixture",
        ),
    )
    chain = OrderedFallbackProvider(
        (FixtureLiveStateProvider((a,)), FixtureLiveStateProvider((b,))),
        categories=(LiveEdgeCategory.ESPORTS_LOL,),
    )
    snaps = chain.snapshots_for(_market(question="Team A vs Team B"), _lol_identity(), now=NOW)
    assert snaps[0].extra.get("source_conflict") is True
    evaluation = evaluate_market(
        _market(question="Team A vs Team B - Series Winner"),
        classify_market(_market(question="Team A vs Team B - Series Winner")),
        FixtureLiveStateProvider(snaps),
        now=NOW,
    )
    assert evaluation.skip_reason == SkipReason.SOURCE_CONFLICT
    result = decide(evaluation, None, now=NOW)
    assert result.decision == LiveEdgeDecision.SKIP


def test_event_alias_wrong_tournament_wrong_date_map():
    market = _market(
        question="LYON vs Team Liquid - Series Winner",
        event_title="LYON vs Team Liquid",
        tags=("lol",),
    )
    identity = classify_market(market)
    aliased = _lol_state(
        sport="League of Legends",
        participants=("LYON (2024 American Team)", "Team Liquid"),
        aliases=("LYON",),
        competition="LCS/2026 Season/Summer Season",
        maps_won=(1, 0),
        current_map=2,
    )
    matched = match_event(market, identity, (aliased,), now=NOW)
    assert matched.status == MatchStatus.VERIFIED_MATCH

    wrong_sport = _lol_state(sport="Counter-Strike 2", participants=("LYON", "Team Liquid"))
    assert match_event(market, identity, (wrong_sport,), now=NOW).status == MatchStatus.NO_MATCH

    far = _lol_state(
        participants=("LYON", "Team Liquid"),
        event_date=NOW - timedelta(days=10),
        start_time=NOW - timedelta(days=10),
    )
    far_match = match_event(market, identity, (far,), now=NOW)
    assert far_match.status != MatchStatus.VERIFIED_MATCH or "дата" in " ".join(far_match.reasons)

    map2 = classify_market(_market(question="Will Team A win Map 2?", event_title="Team A vs Team B"))
    wrong = match_map_market(
        _market(question="Will Team A win Map 2?"), map2, (_lol_state(current_map=3),), now=NOW
    )
    assert wrong.status == MatchStatus.NO_MATCH
    finished = match_map_market(
        _market(question="Will Team A win Map 2?"),
        map2,
        (_lol_state(current_map=1, map_finished=True),),
        now=NOW,
    )
    assert finished.status == MatchStatus.NO_MATCH


def test_evidence_low_blocks_buy():
    weak = _lol_state(
        kills=(6, 5),
        gold_difference=1000,
        towers=(4, 4),
        inhibitors=(0, 0),
        dragons=0,
        barons=0,
        game_time_seconds=600,
        maps_won=(0, 0),
        current_map=1,
    )
    mapped = map_win_probability(weak)
    assert mapped is None or mapped[1] == EvidenceTier.LOW
    market = _market(question="Will Team A win Map 1?")
    evaluation = evaluate_market(
        market, classify_market(market), FixtureLiveStateProvider((weak,)), now=NOW
    )
    assert evaluation.skip_reason is None
    result = decide(evaluation, executable_quote_from_book(_book("0.40"), allocation_usd=Decimal("100"), side="YES", now=NOW), now=NOW)
    assert result.decision == LiveEdgeDecision.WATCH
    assert result.decision != LiveEdgeDecision.BUY_NOW


def test_fee_malformed_unverified():
    with patch("app.opportunity_intel.live_edge.fees.fetch_json", return_value={"foo": 1}):
        fee, note = fetch_taker_fee_usd(
            "token", capital_usd=Decimal("100"), executable_price=Decimal("0.5")
        )
    assert fee is None
    assert note == "Fee estimate unavailable"
    with patch("app.opportunity_intel.live_edge.fees.fetch_json", return_value={"base_fee": "nope"}):
        fee, note = fetch_taker_fee_usd(
            "token", capital_usd=Decimal("100"), executable_price=Decimal("0.5")
        )
    assert fee is None


def test_discovery_pagination_complete_vs_truncated():
    class _Paged:
        def __init__(self, pages):
            self.pages = pages
            self.calls = 0

        def get_json(self, path, params=None):
            self.calls += 1
            idx = self.calls - 1
            if idx >= len(self.pages):
                return {"events": [], "next_cursor": None}
            return self.pages[idx]

    complete = _Paged(
        [
            {"events": [_gamma_event("a", "Weather in NYC?", ["weather"], False)], "next_cursor": "c1"},
            {"events": [_gamma_event("b", "Will Bitcoin hit 100k?", ["crypto"], False)], "next_cursor": None},
        ]
    )
    result = discover_active_markets(complete, now=NOW, max_pages=5)
    assert result.truncated is False
    assert result.pages_fetched == 2
    assert result.status.value == "COMPLETE"

    truncated = _Paged(
        [
            {
                "events": [_gamma_event(f"e{i}", "Q?", ["sports"], True)],
                "next_cursor": f"c{i}",
            }
            for i in range(4)
        ]
    )
    result = discover_active_markets(truncated, now=NOW, max_pages=2)
    assert result.truncated is True
    assert result.pages_fetched == 2


def test_opendota_timeout_none():
    with patch("app.opportunity_intel.live_edge.feeds.opendota.fetch_json", return_value=None):
        assert OpenDotaLiveProvider().snapshots_for(_market(question="x"), _dota_identity(), now=NOW) == ()
