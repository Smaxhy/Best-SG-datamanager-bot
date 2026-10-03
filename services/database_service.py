"""
Async database service — duplicate detection, DM stats, scan history.

Uses SQLAlchemy 2.0 async with aiosqlite (dev) or asyncpg (prod).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import config
from models import Base, ProcessedLog, DataManagerStats, ScanHistory, CommandLog, RankLock, TrainingBan

logger = logging.getLogger("bot.database")

def _engine_kwargs(url: str) -> dict:
    kwargs = {
        "echo": False,
        "pool_recycle": 300,      # recycle connections every 5 min
        "pool_pre_ping": True,    # test connection before using it
    }
    parsed = make_url(url)
    # In-memory SQLite uses a single shared connection (StaticPool),
    # which rejects pool sizing arguments.
    in_memory_sqlite = parsed.get_backend_name() == "sqlite" and parsed.database in (None, "", ":memory:")
    if not in_memory_sqlite:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 5
    return kwargs


engine = create_async_engine(config.DATABASE_URL, **_engine_kwargs(config.DATABASE_URL))
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialised.")


# ── Duplicate detection ──────────────────────────────────────────────

async def is_already_processed(message_id: int) -> bool:
    async with async_session() as session:
        stmt = select(ProcessedLog.id).where(ProcessedLog.discord_message_id == message_id)
        result = await session.execute(stmt)
        return result.scalar() is not None


async def record_processed_log(
    *,
    message_id: int,
    channel_id: int,
    dm_id: int,
    dm_name: str,
    username: str,
    lore_name: str | None,
    ep: int,
    tp: int,
    mp: int,
    detachment: str | None,
    status: str = "success",
    error_detail: str | None = None,
) -> None:
    async with async_session() as session:
        log = ProcessedLog(
            discord_message_id=message_id,
            discord_channel_id=channel_id,
            data_manager_id=dm_id,
            data_manager_name=dm_name,
            username=username,
            lore_name=lore_name,
            ep_change=ep,
            tp_change=tp,
            mp_change=mp,
            detachment=detachment,
            status=status,
            error_detail=error_detail,
        )
        session.add(log)
        await session.commit()


# ── Data Manager weekly stats ────────────────────────────────────────

def _is_expired(expires_at: datetime | None) -> bool:
    """True if *expires_at* has passed. SQLite returns naive datetimes; they are stored as UTC."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at


def _iso_week() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    return iso.year, iso.week


async def increment_dm_score(dm_id: int, dm_name: str, logs: int = 1) -> int:
    """Add *logs* to the DM's current-week tally.  Returns new total."""
    year, week = _iso_week()
    async with async_session() as session:
        stmt = select(DataManagerStats).where(
            DataManagerStats.discord_user_id == dm_id,
            DataManagerStats.iso_year == year,
            DataManagerStats.iso_week == week,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            row = DataManagerStats(
                discord_user_id=dm_id,
                discord_username=dm_name,
                iso_year=year,
                iso_week=week,
                logs_processed=0,
                scans_completed=0,
            )
            session.add(row)
        row.logs_processed += logs
        row.discord_username = dm_name  # keep up to date
        await session.commit()
        return row.logs_processed


async def increment_dm_scans(dm_id: int, dm_name: str) -> None:
    year, week = _iso_week()
    async with async_session() as session:
        stmt = select(DataManagerStats).where(
            DataManagerStats.discord_user_id == dm_id,
            DataManagerStats.iso_year == year,
            DataManagerStats.iso_week == week,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            row = DataManagerStats(
                discord_user_id=dm_id,
                discord_username=dm_name,
                iso_year=year,
                iso_week=week,
                logs_processed=0,
                scans_completed=0,
            )
            session.add(row)
        row.scans_completed += 1
        row.discord_username = dm_name
        await session.commit()


async def get_dm_stats(dm_id: int) -> list[DataManagerStats]:
    """Return all weekly rows for a DM, most recent first."""
    async with async_session() as session:
        stmt = (
            select(DataManagerStats)
            .where(DataManagerStats.discord_user_id == dm_id)
            .order_by(DataManagerStats.iso_year.desc(), DataManagerStats.iso_week.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_dm_current_week(dm_id: int) -> DataManagerStats | None:
    year, week = _iso_week()
    async with async_session() as session:
        stmt = select(DataManagerStats).where(
            DataManagerStats.discord_user_id == dm_id,
            DataManagerStats.iso_year == year,
            DataManagerStats.iso_week == week,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


# ── Scan history ─────────────────────────────────────────────────────

async def create_scan_record(
    dm_id: int, dm_name: str, channel_id: int, channel_name: str
) -> int:
    async with async_session() as session:
        rec = ScanHistory(
            data_manager_id=dm_id,
            data_manager_name=dm_name,
            channel_id=channel_id,
            channel_name=channel_name,
            scan_started=datetime.now(timezone.utc),
        )
        session.add(rec)
        await session.commit()
        return rec.id


async def finish_scan_record(scan_id: int, **kwargs) -> None:
    async with async_session() as session:
        stmt = select(ScanHistory).where(ScanHistory.id == scan_id)
        result = await session.execute(stmt)
        rec = result.scalar_one()
        rec.scan_finished = datetime.now(timezone.utc)
        for k, v in kwargs.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        await session.commit()


# ── Leaderboard queries ──────────────────────────────────────────────

async def get_weekly_leaderboard() -> list[dict]:
    """Top DMs for the current week, sorted by logs processed."""
    year, week = _iso_week()
    async with async_session() as session:
        stmt = (
            select(DataManagerStats)
            .where(
                DataManagerStats.iso_year == year,
                DataManagerStats.iso_week == week,
            )
            .order_by(DataManagerStats.logs_processed.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [
            {
                "user_id": r.discord_user_id,
                "username": r.discord_username,
                "logs": r.logs_processed,
                "scans": r.scans_completed,
            }
            for r in rows
        ]


async def get_alltime_leaderboard() -> list[dict]:
    """Top DMs by total logs across all weeks."""
    from sqlalchemy import func
    async with async_session() as session:
        stmt = (
            select(
                DataManagerStats.discord_user_id,
                DataManagerStats.discord_username,
                func.sum(DataManagerStats.logs_processed).label("total_logs"),
                func.sum(DataManagerStats.scans_completed).label("total_scans"),
            )
            .group_by(DataManagerStats.discord_user_id, DataManagerStats.discord_username)
            .order_by(func.sum(DataManagerStats.logs_processed).desc())
        )
        result = await session.execute(stmt)
        rows = result.all()
        return [
            {
                "user_id": r.discord_user_id,
                "username": r.discord_username,
                "logs": r.total_logs or 0,
                "scans": r.total_scans or 0,
            }
            for r in rows
        ]


# ── Edit DM stats ────────────────────────────────────────────────────

async def set_dm_weekly_logs(dm_id: int, dm_name: str, year: int, week: int, logs: int) -> None:
    """Set a DM's log count for a specific week. Creates the row if needed."""
    async with async_session() as session:
        stmt = select(DataManagerStats).where(
            DataManagerStats.discord_user_id == dm_id,
            DataManagerStats.iso_year == year,
            DataManagerStats.iso_week == week,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            row = DataManagerStats(
                discord_user_id=dm_id,
                discord_username=dm_name,
                iso_year=year,
                iso_week=week,
                logs_processed=logs,
                scans_completed=0,
            )
            session.add(row)
        else:
            row.logs_processed = logs
            row.discord_username = dm_name
        await session.commit()


async def add_dm_logs(dm_id: int, dm_name: str, logs: int) -> int:
    """Add (or subtract) logs from a DM's current week. Returns new total."""
    year, week = _iso_week()
    async with async_session() as session:
        stmt = select(DataManagerStats).where(
            DataManagerStats.discord_user_id == dm_id,
            DataManagerStats.iso_year == year,
            DataManagerStats.iso_week == week,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            row = DataManagerStats(
                discord_user_id=dm_id,
                discord_username=dm_name,
                iso_year=year,
                iso_week=week,
                logs_processed=max(0, logs),
                scans_completed=0,
            )
            session.add(row)
        else:
            row.logs_processed = max(0, row.logs_processed + logs)
            row.discord_username = dm_name
        await session.commit()
        return row.logs_processed

# ── Command logging ──────────────────────────────────────────────────

async def log_command(
    user_id: int, username: str, command: str,
    details: str | None = None, channel_name: str = "",
) -> None:
    async with async_session() as session:
        entry = CommandLog(
            discord_user_id=user_id,
            discord_username=username,
            command=command,
            details=details,
            channel_name=channel_name,
        )
        session.add(entry)
        await session.commit()


async def get_recent_commands(hours: int = 24) -> list[CommandLog]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with async_session() as session:
        stmt = (
            select(CommandLog)
            .where(CommandLog.executed_at >= cutoff)
            .order_by(CommandLog.executed_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

# ── Rank lock ────────────────────────────────────────────────────────

async def ranklock_player(username: str, locked_by_id: int, locked_by_name: str, reason: str | None = None, hours: int | None = None) -> bool:
    """Lock a player. Returns True if newly locked, False if already locked."""
    async with async_session() as session:
        stmt = select(RankLock).where(RankLock.username == username.lower())
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            return False
        expires = datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None
        lock = RankLock(
            username=username.lower(),
            locked_by_id=locked_by_id,
            locked_by_name=locked_by_name,
            reason=reason,
            expires_at=expires,
        )
        session.add(lock)
        await session.commit()
        return True


async def unranklock_player(username: str) -> bool:
    """Unlock a player. Returns True if unlocked, False if wasn't locked."""
    async with async_session() as session:
        stmt = select(RankLock).where(RankLock.username == username.lower())
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if not existing:
            return False
        await session.delete(existing)
        await session.commit()
        return True


async def is_ranklocked(username: str) -> RankLock | None:
    """Check if a player is rank-locked. Auto-removes expired locks."""
    async with async_session() as session:
        stmt = select(RankLock).where(RankLock.username == username.lower())
        result = await session.execute(stmt)
        lock = result.scalar_one_or_none()
        if lock is None:
            return None
        # Check if expired
        if _is_expired(lock.expires_at):
            await session.delete(lock)
            await session.commit()
            return None
        return lock


async def get_all_ranklocks() -> list[RankLock]:
    async with async_session() as session:
        stmt = select(RankLock).order_by(RankLock.locked_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


# ── Training ban functions ──────────────────────────────────────────

async def trainingban_player(
    username: str,
    banned_by_id: int,
    banned_by_name: str,
    discord_id: int | None = None,
    reason: str | None = None,
    hours: int | None = None,
) -> bool:
    """Ban a player from training. Returns True if newly banned, False if already banned."""
    async with async_session() as session:
        stmt = select(TrainingBan).where(TrainingBan.username == username.lower())
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            return False
        expires = datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None
        ban = TrainingBan(
            username=username.lower(),
            discord_id=discord_id,
            banned_by_id=banned_by_id,
            banned_by_name=banned_by_name,
            reason=reason,
            expires_at=expires,
        )
        session.add(ban)
        await session.commit()
        return True


async def trainingunban_player(username: str) -> bool:
    """Unban a player from training. Returns True if unbanned, False if wasn't banned."""
    async with async_session() as session:
        stmt = select(TrainingBan).where(TrainingBan.username == username.lower())
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if not existing:
            return False
        await session.delete(existing)
        await session.commit()
        return True


async def is_trainingbanned(username: str) -> TrainingBan | None:
    """Check if a player is training-banned. Auto-removes expired bans."""
    async with async_session() as session:
        stmt = select(TrainingBan).where(TrainingBan.username == username.lower())
        result = await session.execute(stmt)
        ban = result.scalar_one_or_none()
        if ban is None:
            return None
        if _is_expired(ban.expires_at):
            await session.delete(ban)
            await session.commit()
            return None
        return ban


async def get_all_trainingbans() -> list[TrainingBan]:
    """Get all active training bans."""
    async with async_session() as session:
        stmt = select(TrainingBan).order_by(TrainingBan.banned_at.desc())
        result = await session.execute(stmt)
        bans = list(result.scalars().all())
        # Auto-clean expired
        active = []
        for ban in bans:
            if _is_expired(ban.expires_at):
                await session.delete(ban)
            else:
                active.append(ban)
        await session.commit()
        return active
