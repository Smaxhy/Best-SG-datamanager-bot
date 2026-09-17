"""Tests for database service — duplicate detection, DM stats."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest
import pytest_asyncio

# Override DB to use in-memory SQLite before importing services
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"

from services import database_service as db
from models.base import Base


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create fresh tables for every test."""
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_duplicate_detection():
    assert await db.is_already_processed(111) is False
    await db.record_processed_log(
        message_id=111, channel_id=1, dm_id=1, dm_name="Max",
        username="Smaxhy", lore_name="Jessica Cruz",
        ep=2, tp=1, mp=0, detachment="Rook",
    )
    assert await db.is_already_processed(111) is True
    assert await db.is_already_processed(222) is False


@pytest.mark.asyncio
async def test_dm_score_increments():
    total = await db.increment_dm_score(42, "Max", logs=5)
    assert total == 5
    total = await db.increment_dm_score(42, "Max", logs=3)
    assert total == 8


@pytest.mark.asyncio
async def test_dm_scans_increment():
    await db.increment_dm_scans(42, "Max")
    await db.increment_dm_scans(42, "Max")
    row = await db.get_dm_current_week(42)
    assert row is not None
    assert row.scans_completed == 2


@pytest.mark.asyncio
async def test_scan_history():
    scan_id = await db.create_scan_record(42, "Max", 100, "rook-logs")
    assert scan_id > 0
    await db.finish_scan_record(scan_id, messages_checked=50, logs_processed=10)
