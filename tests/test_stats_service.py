"""Tests for the /dmstats wrapper."""

import pytest

from services import database_service as db
from services import stats_service


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


async def test_current_week_summary_empty():
    assert await stats_service.current_week_summary(42) == {"logs": 0, "scans": 0, "year": 0, "week": 0}


async def test_current_week_summary():
    await db.increment_dm_score(42, "Max", logs=3)
    await db.increment_dm_scans(42, "Max")
    year, week = db._iso_week()
    assert await stats_service.current_week_summary(42) == {"logs": 3, "scans": 1, "year": year, "week": week}


async def test_full_history_newest_first():
    await db.set_dm_weekly_logs(42, "Max", 2025, 1, 2)
    await db.set_dm_weekly_logs(42, "Max", 2025, 3, 5)
    assert await stats_service.full_history(42) == [
        {"year": 2025, "week": 3, "logs": 5, "scans": 0},
        {"year": 2025, "week": 1, "logs": 2, "scans": 0},
    ]
