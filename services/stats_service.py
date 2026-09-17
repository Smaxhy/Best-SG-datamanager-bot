"""Thin wrapper over database queries for the /dmstats command."""

from __future__ import annotations
from services.database_service import get_dm_stats, get_dm_current_week


async def current_week_summary(dm_id: int) -> dict:
    row = await get_dm_current_week(dm_id)
    if row is None:
        return {"logs": 0, "scans": 0, "year": 0, "week": 0}
    return {
        "logs": row.logs_processed,
        "scans": row.scans_completed,
        "year": row.iso_year,
        "week": row.iso_week,
    }


async def full_history(dm_id: int) -> list[dict]:
    rows = await get_dm_stats(dm_id)
    return [
        {
            "year": r.iso_year,
            "week": r.iso_week,
            "logs": r.logs_processed,
            "scans": r.scans_completed,
        }
        for r in rows
    ]
