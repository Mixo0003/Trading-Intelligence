"""Production live-feed parser tests. HTTP is mocked — no invented live data."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from app.opportunity_intel.live_edge.classify import classify_market
from app.opportunity_intel.live_edge.evaluators import evaluate_market
from app.opportunity_intel.live_edge.feeds.liquipedia import LiquipediaMatchProvider
from app.opportunity_intel.live_edge.feeds.mlb import MlbStatsProvider
from app.opportunity_intel.live_edge.feeds.opendota import OpenDotaLiveProvider
from app.opportunity_intel.live_edge.feeds.sportsdb import TheSportsDbProvider
from app.opportunity_intel.live_edge.fees import fetch_taker_fee_usd
from app.opportunity_intel.live_edge.http_json import clear_live_edge_http_cache
from app.opportunity_intel.live_edge.models import LiveEdgeCategory, SkipReason
from app.opportunity_intel.live_edge.probability import series_win_probability
from decimal import Decimal
from tests.opportunity_intel.test_v244_live_edge import NOW, _market

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_live_edge_http_cache()
    yield
    clear_live_edge_http_cache()


def test_sportsdb_parse_and_stale_malformed():
    soccer = classify_market(
        _market(
            question="San Jose Earthquakes II vs Los Angeles FC II",
            event_title="San Jose Earthquakes II vs Los Angeles FC II",
            tags=("soccer",),
            category_ids=("sports",),
        )
    )
    payload = {
        "livescore": [
            {
                "idEvent": "2423392",
                "strHomeTeam": "San Jose Earthquakes II",
                "strAwayTeam": "Los Angeles FC II",
                "intHomeScore": "2",
                "intAwayScore": "0",
                "strProgress": "88",
                "strStatus": "1H",
                "strLeague": "MLS Next Pro",
                "strSport": "Soccer",
            }
        ]
    }
    with patch(
        "app.opportunity_intel.live_edge.feeds.sportsdb.fetch_json",
        return_value=payload,
    ):
        snaps = TheSportsDbProvider().snapshots_for(
            _market(question="x", tags=("sports",)), soccer, now=NOW
        )
    assert len(snaps) == 1
    assert snaps[0].score == (2, 0)
    assert snaps[0].provenance.provider == "thesportsdb-livescore"
    with patch(
        "app.opportunity_intel.live_edge.feeds.sportsdb.fetch_json",
        return_value={"livescore": "bad"},
    ):
        assert TheSportsDbProvider().snapshots_for(
            _market(question="x"), soccer, now=NOW
        ) == ()
    with patch(
        "app.opportunity_intel.live_edge.feeds.sportsdb.fetch_json",
        return_value=None,
    ):
        assert TheSportsDbProvider().snapshots_for(
            _market(question="x"), soccer, now=NOW
        ) == ()


def test_mlb_live_only_and_wrong_category():
    baseball = classify_market(
        _market(
            question="Yankees vs Red Sox",
            event_title="New York Yankees vs Boston Red Sox",
            tags=("mlb",),
            category_ids=("sports",),
        )
    )
    payload = {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 1,
                        "status": {"abstractGameState": "Preview"},
                        "teams": {
                            "home": {"team": {"name": "Yankees", "abbreviation": "NYY"}, "score": 0},
                            "away": {"team": {"name": "Red Sox", "abbreviation": "BOS"}, "score": 0},
                        },
                    },
                    {
                        "gamePk": 2,
                        "status": {"abstractGameState": "Live"},
                        "teams": {
                            "home": {"team": {"name": "Yankees", "abbreviation": "NYY"}, "score": 7},
                            "away": {"team": {"name": "Red Sox", "abbreviation": "BOS"}, "score": 1},
                        },
                        "linescore": {
                            "currentInning": 8,
                            "inningState": "Bottom",
                            "outs": 2,
                        },
                    },
                ]
            }
        ]
    }
    with patch("app.opportunity_intel.live_edge.feeds.mlb.fetch_json", return_value=payload):
        snaps = MlbStatsProvider().snapshots_for(
            _market(question="Yankees vs Red Sox", event_title="Yankees vs Red Sox"),
            baseball,
            now=NOW,
        )
    assert len(snaps) == 1
    assert snaps[0].inning == 8
    soccer = classify_market(_market(question="Arsenal vs Chelsea", tags=("soccer",)))
    with patch("app.opportunity_intel.live_edge.feeds.mlb.fetch_json", return_value=payload):
        assert MlbStatsProvider().snapshots_for(_market(question="x"), soccer, now=NOW) == ()


def test_opendota_requires_team_names():
    dota = classify_market(
        _market(
            question="Team Secret vs OG - Series Winner",
            event_title="Team Secret vs OG",
            tags=("dota 2",),
        )
    )
    payload = [
        {"match_id": 1, "team_name_radiant": "", "team_name_dire": "", "game_time": 100},
        {
            "match_id": 2,
            "team_name_radiant": "Team Secret",
            "team_name_dire": "OG",
            "game_time": 1800,
            "delay": 110,
            "scoreboard": {
                "radiant": {"score": 20, "gold": 40000, "tower_state": 7},
                "dire": {"score": 5, "gold": 20000, "tower_state": 3},
            },
        },
        {
            "match_id": 3,
            "radiant_team": {"name": "Gaimin Gladiators"},
            "dire_team": {"name": "Team Spirit"},
            "game_time": 600,
        },
    ]
    with patch(
        "app.opportunity_intel.live_edge.feeds.opendota.fetch_json", return_value=payload
    ):
        snaps = OpenDotaLiveProvider().snapshots_for(_market(question="x"), dota, now=NOW)
    assert len(snaps) == 2
    assert snaps[0].kills == (20, 5)
    assert snaps[0].provenance.provider == "opendota-live"
    assert snaps[1].participants == ("Gaimin Gladiators", "Team Spirit")


def test_liquipedia_malformed_and_timeout():
    lol = classify_market(
        _market(question="T1 vs Gen.G - Series Winner", event_title="T1 vs Gen.G", tags=("lol",))
    )
    with patch(
        "app.opportunity_intel.live_edge.feeds.liquipedia.fetch_json", return_value=None
    ):
        assert LiquipediaMatchProvider().snapshots_for(_market(question="x"), lol, now=NOW) == ()
    with patch(
        "app.opportunity_intel.live_edge.feeds.liquipedia.fetch_json",
        return_value={"cargoquery": [{"title": {"Opponent": "T1", "Opponent2": "Gen.G", "Bestof": "5", "Score": "2-0", "Status": "live"}}]},
    ):
        snaps = LiquipediaMatchProvider().snapshots_for(_market(question="x"), lol, now=NOW)
    assert len(snaps) == 1
    assert snaps[0].maps_won == (2, 0)
    assert snaps[0].series_format == "BO5"


def test_fee_unavailable_when_endpoint_fails():
    with patch("app.opportunity_intel.live_edge.fees.fetch_json", return_value=None):
        fee, note = fetch_taker_fee_usd(
            "token", capital_usd=Decimal("100"), executable_price=Decimal("0.8")
        )
    assert fee is None
    assert note == "Fee estimate unavailable"
    with patch(
        "app.opportunity_intel.live_edge.fees.fetch_json",
        return_value={"base_fee": 0},
    ):
        fee, note = fetch_taker_fee_usd(
            "token", capital_usd=Decimal("100"), executable_price=Decimal("0.5")
        )
    assert fee == Decimal("0")


def test_series_combinatorics_not_hardcoded_two_oh():
    result = series_win_probability((2, 0), best_of=5, current_map_p=None)
    assert result is not None
    probability, tier = result
    assert probability.low < Decimal("0.92")
    assert probability.low > Decimal("0.80")
    assert tier.value in {"MEDIUM", "LOW"}


def test_http_429_and_timeout_fail_closed():
    identity = classify_market(_market(question="A vs B", tags=("soccer",)))
    with patch(
        "app.opportunity_intel.live_edge.feeds.sportsdb.fetch_json", return_value=None
    ):
        evaluation = evaluate_market(
            _market(question="A vs B", event_title="A vs B", tags=("soccer",)),
            identity,
            TheSportsDbProvider(),
            now=NOW,
        )
    assert evaluation.skip_reason in {
        SkipReason.INSUFFICIENT_DATA,
        SkipReason.NO_MATCH,
    }
    assert evaluation.probability is None
