import os
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.database.storage import (
    get_connection,
    get_recent_analyses,
    initialize_database,
    save_analysis_record,
    upsert_user,
)
from app.region.types import ScanRegion
from app.signals.market_analysis import ScanProgress
from app.telegram.scan_cache import load_latest_scan_snapshot, save_scan_snapshot
from tests.conftest import make_test_settings


def _settings(tmp_path):
    return make_test_settings(database_path=str(tmp_path / "connections.db"))


def test_database_reopens_and_can_be_renamed_after_operations(tmp_path):
    settings = _settings(tmp_path)
    initialize_database(settings)
    upsert_user(settings, 42, "user", "Test")
    save_analysis_record(
        settings,
        42,
        "https://polymarket.com/event/test",
        "Test",
        "FORECAST ONLY",
        0.5,
    )
    save_scan_snapshot(
        settings,
        42,
        ScanProgress(scan_region=ScanRegion.USA.value),
        region=ScanRegion.USA,
    )
    assert get_recent_analyses(settings)
    assert load_latest_scan_snapshot(settings, 42, region=ScanRegion.USA) is not None

    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1

    renamed = tmp_path / "renamed.db"
    os.replace(settings.database_path, renamed)
    assert renamed.exists()
    renamed.unlink()
    assert not renamed.exists()


def test_concurrent_read_handlers_leave_no_database_lock(tmp_path):
    settings = _settings(tmp_path)
    initialize_database(settings)
    upsert_user(settings, 42, "user", "Test")
    save_scan_snapshot(
        settings,
        42,
        ScanProgress(scan_region=ScanRegion.ALL.value),
        region=ScanRegion.ALL,
    )

    def read_once():
        get_recent_analyses(settings)
        return load_latest_scan_snapshot(settings, 42, region=ScanRegion.ALL)

    with ThreadPoolExecutor(max_workers=4) as executor:
        snapshots = list(executor.map(lambda _: read_once(), range(12)))
    assert all(snapshot is not None for snapshot in snapshots)

    renamed = tmp_path / "after-concurrent-reads.db"
    os.replace(settings.database_path, renamed)
    renamed.unlink()


def test_exception_rolls_back_and_closes_connection(tmp_path):
    settings = _settings(tmp_path)
    initialize_database(settings)

    with pytest.raises(RuntimeError):
        with get_connection(settings) as connection:
            connection.execute(
                """
                INSERT INTO users (
                    telegram_id, username, first_name, language, created_at, last_seen_at
                ) VALUES (1, 'u', 'n', 'en', datetime('now'), datetime('now'))
                """
            )
            raise RuntimeError("force rollback")

    with get_connection(settings) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    renamed = tmp_path / "after-exception.db"
    os.replace(settings.database_path, renamed)
    renamed.unlink()
