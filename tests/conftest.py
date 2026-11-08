"""Shared test setup: in-memory DB, import path, and a fresh schema per test."""

import os
import sys

# Must be set before any service module is imported.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio

from models.base import Base
from services import database_service as db


@pytest_asyncio.fixture
async def fresh_db():
    """Create empty tables for a test, drop them afterwards."""
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _fresh_sheet_lock(monkeypatch):
    """asyncio locks bind to one event loop; each test runs on its own."""
    import asyncio
    from services import sheets_service

    monkeypatch.setattr(sheets_service, "_write_lock", asyncio.Lock())
