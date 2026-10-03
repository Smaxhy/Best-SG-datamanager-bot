"""Tests for database service — duplicate detection, DM stats, locks, bans."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from models import RankLock, TrainingBan
from services import database_service as db


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


async def _expire(model, username: str) -> None:
    """Move a lock/ban's expiry into the past."""
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with db.async_session() as session:
        await session.execute(
            update(model).where(model.username == username.lower()).values(expires_at=past)
        )
        await session.commit()


# ── Engine config ────────────────────────────────────────────────────

def test_engine_kwargs_skip_pool_sizing_for_in_memory_sqlite():
    kwargs = db._engine_kwargs("sqlite+aiosqlite://")
    assert "pool_size" not in kwargs
    assert "max_overflow" not in kwargs


@pytest.mark.parametrize("url", [
    "sqlite+aiosqlite:///./data/bot.db",
    "postgresql+asyncpg://user:pw@host/db",
])
def test_engine_kwargs_keep_pool_sizing_otherwise(url):
    kwargs = db._engine_kwargs(url)
    assert kwargs["pool_size"] == 5
    assert kwargs["max_overflow"] == 5
    assert kwargs["pool_pre_ping"] is True


# ── Expiry helper ────────────────────────────────────────────────────

def test_is_expired_handles_none_naive_and_aware():
    now = datetime.now(timezone.utc)
    assert db._is_expired(None) is False
    assert db._is_expired(now + timedelta(hours=1)) is False
    assert db._is_expired(now - timedelta(hours=1)) is True
    # SQLite hands back naive datetimes (stored as UTC)
    assert db._is_expired((now + timedelta(hours=1)).replace(tzinfo=None)) is False
    assert db._is_expired((now - timedelta(hours=1)).replace(tzinfo=None)) is True


# ── Duplicate detection ──────────────────────────────────────────────

async def test_duplicate_detection():
    assert await db.is_already_processed(111) is False
    await db.record_processed_log(
        message_id=111, channel_id=1, dm_id=1, dm_name="Max",
        username="Smaxhy", lore_name="Jessica Cruz",
        ep=2, tp=1, mp=0, detachment="Rook",
    )
    assert await db.is_already_processed(111) is True
    assert await db.is_already_processed(222) is False


async def test_recording_same_message_twice_raises():
    kwargs = dict(
        message_id=111, channel_id=1, dm_id=1, dm_name="Max",
        username="Smaxhy", lore_name=None, ep=1, tp=0, mp=0, detachment=None,
    )
    await db.record_processed_log(**kwargs)
    with pytest.raises(Exception):
        await db.record_processed_log(**kwargs)


# ── DM stats ─────────────────────────────────────────────────────────

async def test_dm_score_increments():
    total = await db.increment_dm_score(42, "Max", logs=5)
    assert total == 5
    total = await db.increment_dm_score(42, "Max", logs=3)
    assert total == 8


async def test_dm_score_updates_display_name():
    await db.increment_dm_score(42, "OldName")
    await db.increment_dm_score(42, "NewName")
    row = await db.get_dm_current_week(42)
    assert row.discord_username == "NewName"


async def test_dm_scans_increment():
    await db.increment_dm_scans(42, "Max")
    await db.increment_dm_scans(42, "Max")
    row = await db.get_dm_current_week(42)
    assert row is not None
    assert row.scans_completed == 2


async def test_add_dm_logs_never_goes_below_zero():
    assert await db.add_dm_logs(42, "Max", -5) == 0
    assert await db.add_dm_logs(42, "Max", 4) == 4
    assert await db.add_dm_logs(42, "Max", -10) == 0


async def test_set_dm_weekly_logs_creates_and_overwrites():
    await db.set_dm_weekly_logs(42, "Max", 2025, 10, 7)
    await db.set_dm_weekly_logs(42, "Max", 2025, 10, 3)
    await db.set_dm_weekly_logs(42, "Max", 2025, 11, 9)
    rows = await db.get_dm_stats(42)
    assert [(r.iso_year, r.iso_week, r.logs_processed) for r in rows] == [
        (2025, 11, 9),
        (2025, 10, 3),
    ]


async def test_weekly_leaderboard_sorted_by_logs():
    await db.increment_dm_score(1, "A", logs=2)
    await db.increment_dm_score(2, "B", logs=9)
    await db.increment_dm_score(3, "C", logs=5)
    board = await db.get_weekly_leaderboard()
    assert [r["username"] for r in board] == ["B", "C", "A"]
    assert board[0]["logs"] == 9


async def test_weekly_leaderboard_ignores_other_weeks():
    await db.set_dm_weekly_logs(1, "Old", 2000, 1, 100)
    await db.increment_dm_score(2, "Current", logs=1)
    board = await db.get_weekly_leaderboard()
    assert [r["username"] for r in board] == ["Current"]


async def test_alltime_leaderboard_sums_weeks():
    await db.set_dm_weekly_logs(1, "A", 2025, 1, 4)
    await db.set_dm_weekly_logs(1, "A", 2025, 2, 6)
    await db.set_dm_weekly_logs(2, "B", 2025, 1, 7)
    board = await db.get_alltime_leaderboard()
    assert [(r["username"], r["logs"]) for r in board] == [("A", 10), ("B", 7)]


# ── Scan history ─────────────────────────────────────────────────────

async def test_scan_history():
    scan_id = await db.create_scan_record(42, "Max", 100, "rook-logs")
    assert scan_id > 0
    await db.finish_scan_record(scan_id, messages_checked=50, logs_processed=10)


async def test_finish_scan_record_ignores_unknown_fields():
    scan_id = await db.create_scan_record(42, "Max", 100, "rook-logs")
    await db.finish_scan_record(scan_id, not_a_column=1, messages_checked=3)


# ── Command log ──────────────────────────────────────────────────────

async def test_log_command_and_read_back():
    await db.log_command(1, "Max", "scan", details="rook", channel_name="logs")
    recent = await db.get_recent_commands(hours=1)
    assert [c.command for c in recent] == ["scan"]


# ── Rank lock ────────────────────────────────────────────────────────

async def test_ranklock_lifecycle_is_case_insensitive():
    assert await db.ranklock_player("Bob", 1, "Max", reason="test") is True
    assert await db.ranklock_player("BOB", 1, "Max") is False
    lock = await db.is_ranklocked("bOb")
    assert lock is not None and lock.reason == "test"
    assert len(await db.get_all_ranklocks()) == 1
    assert await db.unranklock_player("bob") is True
    assert await db.unranklock_player("bob") is False
    assert await db.is_ranklocked("Bob") is None


async def test_timed_ranklock_is_active_before_expiry():
    await db.ranklock_player("Bob", 1, "Max", hours=1)
    assert await db.is_ranklocked("Bob") is not None


async def test_expired_ranklock_is_removed():
    await db.ranklock_player("Bob", 1, "Max", hours=1)
    await _expire(RankLock, "Bob")
    assert await db.is_ranklocked("Bob") is None
    assert await db.get_all_ranklocks() == []


# ── Training ban ─────────────────────────────────────────────────────

async def test_trainingban_lifecycle_is_case_insensitive():
    assert await db.trainingban_player("Bob", 1, "Max", discord_id=99, reason="afk") is True
    assert await db.trainingban_player("bob", 1, "Max") is False
    ban = await db.is_trainingbanned("BOB")
    assert ban is not None and ban.discord_id == 99
    assert await db.trainingunban_player("Bob") is True
    assert await db.trainingunban_player("Bob") is False
    assert await db.is_trainingbanned("Bob") is None


async def test_timed_trainingban_is_active_before_expiry():
    await db.trainingban_player("Bob", 1, "Max", hours=2)
    assert await db.is_trainingbanned("Bob") is not None
    assert len(await db.get_all_trainingbans()) == 1


async def test_expired_trainingban_is_removed():
    await db.trainingban_player("Bob", 1, "Max", hours=1)
    await db.trainingban_player("Alice", 1, "Max")
    await _expire(TrainingBan, "Bob")
    active = await db.get_all_trainingbans()
    assert [b.username for b in active] == ["alice"]
    assert await db.is_trainingbanned("Bob") is None
