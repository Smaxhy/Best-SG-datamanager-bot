"""
Configuration module — loads all settings from environment variables.
Never hard-code secrets or IDs here.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Discord ──────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DATA_MANAGER_ROLE_ID = int(os.getenv("DATA_MANAGER_ROLE_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
NCO_ROLE_ID = int(os.getenv("NCO_ROLE_ID", "0"))

# Optional: restrict which channels can be scanned (comma-separated IDs)
# Leave empty to allow any channel the bot can read.
ALLOWED_CHANNEL_IDS_RAW = os.getenv("ALLOWED_CHANNEL_IDS", "")
ALLOWED_CHANNEL_IDS: list[int] = (
    [int(cid.strip()) for cid in ALLOWED_CHANNEL_IDS_RAW.split(",") if cid.strip()]
    if ALLOWED_CHANNEL_IDS_RAW
    else []
)

# ── Google Sheets ────────────────────────────────────────────────────
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
# If the JSON is supplied directly as an env var (for cloud hosts):
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# Player tabs to search (comma-separated in env).
_DEFAULT_SHEETS = "[D] ROOK,[D] BASTION,[C] ANCILLARY"
_SHEETS_RAW = os.getenv("PLAYER_SHEETS", _DEFAULT_SHEETS)
DETACHMENT_SHEETS: list[str] = [s.strip() for s in _SHEETS_RAW.split(",") if s.strip()]

# ── Scan settings ────────────────────────────────────────────────────
SCAN_HOURS = int(os.getenv("SCAN_HOURS", "48"))

# ── Database ─────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/bot.db")

# ── Valid game constants ─────────────────────────────────────────────
VALID_RANKS = {"RCT", "PVT", "PFC", "LCPL"}
