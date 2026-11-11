"""config.py must survive blank env vars (Railway allows them) and plain postgres URLs."""

import importlib

import pytest

import config


@pytest.fixture
def reload_config(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)

    def _reload(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config)

    yield _reload
    monkeypatch.undo()
    importlib.reload(config)


def test_blank_role_ids_and_scan_hours_fall_back(reload_config):
    cfg = reload_config(DATA_MANAGER_ROLE_ID="", STAFF_ROLE_ID=" ", NCO_ROLE_ID="", SCAN_HOURS="")
    assert (cfg.DATA_MANAGER_ROLE_ID, cfg.STAFF_ROLE_ID, cfg.NCO_ROLE_ID) == (0, 0, 0)
    assert cfg.SCAN_HOURS == 48


@pytest.mark.parametrize("url", [
    "postgres://u:p@host:5432/railway",
    "postgresql://u:p@host:5432/railway",
])
def test_plain_postgres_url_gets_async_driver(reload_config, url):
    assert reload_config(DATABASE_URL=url).DATABASE_URL == "postgresql+asyncpg://u:p@host:5432/railway"


def test_explicit_async_url_left_alone(reload_config):
    url = "postgresql+asyncpg://u:p@host/db"
    assert reload_config(DATABASE_URL=url).DATABASE_URL == url


def test_blank_database_url_uses_sqlite_default(reload_config):
    assert reload_config(DATABASE_URL="").DATABASE_URL.startswith("sqlite+aiosqlite")
