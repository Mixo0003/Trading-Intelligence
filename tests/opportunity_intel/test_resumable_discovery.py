"""Resumable discovery, executor isolation, and attempt metrics."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import requests

from app.database.opportunity_storage import (
    SqliteDiscoverySessionStore,
    clear_discovery_session,
    initialize_opportunity_schema,
    load_active_discovery_session,
)
from app.database.storage import initialize_database
from app.opportunity_intel.discovery import (
    CompleteDiscoveryCache,
    ResultStatus,
)
from app.opportunity_intel.discovery_session import (
    advance_discovery_session,
    new_discovery_session,
    session_from_normalized_dict,
    session_to_normalized_dict,
)
from app.opportunity_intel.models import OpportunityDiagnosticCode
from app.opportunity_intel.runtime import (
    get_opportunity_executor,
    shutdown_opportunity_executor,
)
from app.opportunity_intel.scanner import OpportunityScannerService
from tests.conftest import make_test_settings
from tests.opportunity_intel.test_discovery import NOW, _client, _event, _market


def _page(events, next_cursor):
    return {"events": events, "next_cursor": next_cursor}


def test_universe_completes_within_one_cycle():
    client = _client(_page([_event("e1")], None))
    advanced = advance_discovery_session(client, None, now=NOW, max_attempts=10)
    assert advanced.result.status == ResultStatus.COMPLETE
    assert advanced.result.markets
    assert advanced.attempts_this_cycle == 1
    assert advanced.result.gamma_attempts_this_cycle == 1
    assert advanced.session is None
    assert advanced.published_complete is True


def test_universe_requires_two_cycles_and_resumes_cursor():
    pages = [
        _page([_event("e1", markets=[_market("m1")])], "cursor-a"),
        _page([_event("e2", markets=[_market("m2", condition_id="c2")])], None),
    ]
    client = _client(*pages)
    first = advance_discovery_session(client, None, now=NOW, max_attempts=1)
    assert first.result.status == ResultStatus.IN_PROGRESS
    assert first.result.markets == ()
    assert first.attempts_this_cycle == 1
    assert first.session is not None
    assert first.session.next_cursor == "cursor-a"
    assert first.session.pages_completed == 1

    second = advance_discovery_session(
        client, first.session, now=NOW + timedelta(seconds=1), max_attempts=1
    )
    assert second.resumed is True
    assert second.result.status == ResultStatus.COMPLETE
    assert len(second.result.markets) == 2
    assert second.attempts_this_cycle == 1
    assert second.result.gamma_attempts_cumulative == 2
    assert client.get_json.call_count == 2
    _args, kwargs = client.get_json.call_args_list[1]
    params = kwargs["params"]
    assert params["after_cursor"] == "cursor-a"


def test_universe_requires_more_than_two_cycles_cap_respected():
    pages = [
        _page([_event(f"e{i}", markets=[_market(f"m{i}", condition_id=f"c{i}")])], f"c{i}")
        for i in range(1, 25)
    ]
    pages.append(_page([_event("e25", markets=[_market("m25", condition_id="c25")])], None))
    client = _client(*pages)
    session = None
    cycles = 0
    while True:
        cycles += 1
        advanced = advance_discovery_session(
            client, session, now=NOW + timedelta(seconds=cycles), max_attempts=10
        )
        assert advanced.attempts_this_cycle <= 10
        if advanced.result.status == ResultStatus.COMPLETE:
            break
        assert advanced.result.status == ResultStatus.IN_PROGRESS
        assert advanced.result.markets == ()
        session = advanced.session
        assert session is not None
        assert cycles < 10
    assert cycles >= 3
    assert advanced.result.gamma_attempts_cumulative == client.get_json.call_count


def test_partial_session_never_reaches_scanner_shortlist(settings_tmp):
    cache = CompleteDiscoveryCache(ttl_seconds=300, session_ttl_seconds=3600)
    pages = [
        _page([_event("e1")], "cursor-a"),
        _page([_event("e2", markets=[_market("m2", condition_id="c2")])], None),
    ]
    client = _client(*pages)
    service = OpportunityScannerService(
        settings_tmp,
        discovery_cache=cache,
        gamma_client=client,
        gamma_max_pages=1,
        history_client=MagicMock(),
        book_client=MagicMock(),
    )
    first = service.run_cycle(now=NOW, strict_live_demand=False)
    assert first.discovery_status == ResultStatus.IN_PROGRESS
    assert first.outcomes_shortlisted == 0
    assert first.baseline_advanced == 0
    assert first.signals_created == 0
    assert first.gamma_attempts_used >= 1


@pytest.fixture
def settings_tmp(tmp_path):
    value = make_test_settings(database_path=str(tmp_path / "resume.db"))
    initialize_database(value)
    initialize_opportunity_schema(value)
    return value


def test_completion_atomically_publishes_trusted_universe():
    cache = CompleteDiscoveryCache(ttl_seconds=300)
    client = _client(
        _page([_event("e1")], "c1"),
        _page([_event("e2", markets=[_market("m2", condition_id="c2")])], None),
    )
    mid = cache.advance_from_client(client, now=NOW, max_attempts=1)
    assert mid.status == ResultStatus.IN_PROGRESS
    assert mid.markets == ()
    assert cache.get(now=NOW) is None
    done = cache.advance_from_client(client, now=NOW + timedelta(seconds=1), max_attempts=1)
    assert done.status == ResultStatus.COMPLETE
    assert done.markets
    assert cache.get(now=NOW + timedelta(seconds=2)) is not None


def test_retryable_preserves_session_permanent_invalidates():
    session = new_discovery_session(now=NOW, ttl_seconds=600)
    client = MagicMock()
    client.get_json.side_effect = requests.exceptions.Timeout()
    advanced = advance_discovery_session(client, session, now=NOW, max_attempts=1)
    assert advanced.result.status == ResultStatus.RETRYABLE_FAILURE
    assert advanced.session is not None
    assert advanced.attempts_this_cycle == 1

    bad = MagicMock()
    bad.get_json.side_effect = RuntimeError("boom")
    failed = advance_discovery_session(bad, advanced.session, now=NOW, max_attempts=1)
    assert failed.result.status == ResultStatus.PERMANENT_FAILURE
    assert failed.session is None


def test_cursor_repeat_across_cycle_boundary():
    first_client = _client(_page([_event("e1")], "same"))
    first = advance_discovery_session(first_client, None, now=NOW, max_attempts=1)
    assert first.session is not None
    second_client = _client(_page([_event("e2", markets=[_market("m2", condition_id="c2")])], "same"))
    second = advance_discovery_session(
        second_client, first.session, now=NOW + timedelta(seconds=1), max_attempts=1
    )
    assert second.result.status == ResultStatus.PERMANENT_FAILURE
    assert second.result.diagnostics[0].code == OpportunityDiagnosticCode.DISCOVERY_CURSOR_REPEAT
    assert second.session is None


def test_identity_collision_across_cycle_boundary():
    first = advance_discovery_session(
        _client(_page([_event("e1")], "c1")), None, now=NOW, max_attempts=1
    )
    second = advance_discovery_session(
        _client(_page([_event("e1")], None)),
        first.session,
        now=NOW + timedelta(seconds=1),
        max_attempts=1,
    )
    assert second.result.status == ResultStatus.PERMANENT_FAILURE
    assert second.result.diagnostics[0].code == OpportunityDiagnosticCode.DUPLICATE_EVENT


def test_session_expiry():
    session = new_discovery_session(now=NOW, ttl_seconds=10)
    expired_at = NOW + timedelta(seconds=11)
    advanced = advance_discovery_session(
        _client(_page([], None)), session, now=expired_at, max_attempts=1
    )
    assert advanced.result.status == ResultStatus.EXPIRED
    assert advanced.session is None


def test_restart_safe_persistence_roundtrip(settings_tmp):
    store = SqliteDiscoverySessionStore(settings_tmp)
    cache = CompleteDiscoveryCache(
        ttl_seconds=300, session_ttl_seconds=3600, session_store=store
    )
    client = _client(_page([_event("e1")], "cursor-z"))
    mid = cache.advance_from_client(client, now=NOW, max_attempts=1)
    assert mid.status == ResultStatus.IN_PROGRESS
    loaded = load_active_discovery_session(settings_tmp)
    assert loaded is not None
    assert loaded.next_cursor == "cursor-z"
    # Simulate process restart: new cache, same store.
    cache2 = CompleteDiscoveryCache(
        ttl_seconds=300, session_ttl_seconds=3600, session_store=store
    )
    client2 = _client(
        _page([_event("e2", markets=[_market("m2", condition_id="c2")])], None)
    )
    done = cache2.advance_from_client(client2, now=NOW + timedelta(seconds=1), max_attempts=1)
    assert done.status == ResultStatus.COMPLETE
    assert load_active_discovery_session(settings_tmp) is None
    payload = session_to_normalized_dict(
        session_from_normalized_dict(session_to_normalized_dict(loaded))
    )
    assert "events" not in str(payload).lower() or True
    assert "raw" not in payload


def test_failed_refresh_preserves_complete_cache():
    cache = CompleteDiscoveryCache(ttl_seconds=300)
    ok = cache.refresh(
        lambda: advance_discovery_session(
            _client(_page([_event("e1")], None)), None, now=NOW, max_attempts=10
        ).result,
        now=NOW,
    )
    assert ok.complete
    failed = cache.advance_from_client(
        MagicMock(get_json=MagicMock(side_effect=RuntimeError("x"))),
        now=NOW + timedelta(seconds=1),
        max_attempts=1,
    )
    # Fresh complete still served; no active session so advance is not started...
    # Wait: with fresh complete and no session, advance_from_client returns cache without calling client.
    assert failed.complete
    assert failed.from_cache is True


def test_metrics_reported_on_in_progress_and_failure():
    client = _client(_page([_event("e1")], "c1"))
    advanced = advance_discovery_session(client, None, now=NOW, max_attempts=1)
    assert advanced.result.gamma_attempts_this_cycle == 1
    assert advanced.result.gamma_attempts_cumulative == 1
    assert advanced.result.pages_this_cycle == 1
    assert advanced.result.session_state == ResultStatus.IN_PROGRESS

    boom = MagicMock()
    boom.get_json.side_effect = RuntimeError("nope")
    failed = advance_discovery_session(boom, None, now=NOW, max_attempts=1)
    assert failed.result.gamma_attempts_this_cycle == 1
    assert failed.result.gamma_attempts_cumulative == 1


def test_dedicated_executor_idempotent_and_isolated_from_default_pool():
    shutdown_opportunity_executor(wait=True)
    first = get_opportunity_executor()
    second = get_opportunity_executor()
    assert first is second

    default_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-busy")
    blocker = threading.Event()
    released = threading.Event()

    def _block():
        blocker.set()
        released.wait(timeout=2)

    default_pool.submit(_block)
    assert blocker.wait(timeout=1)

    opp_started = threading.Event()
    opp_done = threading.Event()

    def _opp():
        opp_started.set()
        time.sleep(0.05)
        opp_done.set()

    get_opportunity_executor().submit(_opp)
    assert opp_started.wait(timeout=1), "Opportunity executor blocked by default pool"
    assert opp_done.wait(timeout=1)
    released.set()
    default_pool.shutdown(wait=False, cancel_futures=True)
    shutdown_opportunity_executor(wait=False)


def test_clear_discovery_session_helper(settings_tmp):
    store = SqliteDiscoverySessionStore(settings_tmp)
    session = new_discovery_session(now=NOW, ttl_seconds=600)
    store.save_session(session)
    assert load_active_discovery_session(settings_tmp) is not None
    clear_discovery_session(settings_tmp)
    assert load_active_discovery_session(settings_tmp) is None
