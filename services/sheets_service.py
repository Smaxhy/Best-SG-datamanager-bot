"""
Google Sheets service — finds players by header name, reads/writes stats.

Uses gspread_asyncio + google-auth for async, non-blocking access.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from gspread_asyncio import AsyncioGspreadClientManager

import config
from models.player import PlayerRow

logger = logging.getLogger("bot.sheets")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _get_credentials() -> Credentials:
    """Build credentials from env-var JSON or a file on disk."""
    if config.GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    return creds


agcm = AsyncioGspreadClientManager(_get_credentials)


# ── Internal helpers ─────────────────────────────────────────────────

async def _get_worksheet(sheet_name: str) -> gspread.Worksheet:
    client = await agcm.authorize()
    ss = await client.open_by_key(config.GOOGLE_SPREADSHEET_ID)
    return await ss.worksheet(sheet_name)


async def _header_map(ws) -> dict[str, int]:
    """Return {lower-cased header: 0-based col index} for row 1."""
    row = await ws.row_values(1)
    return {cell.strip().lower(): idx for idx, cell in enumerate(row)}


# ── Public API ───────────────────────────────────────────────────────

async def find_player(username: str) -> PlayerRow | None:
    """
    Search every detachment sheet for *username* (case-insensitive).
    Returns the first match or None.
    """
    target = username.strip().lower()

    for sheet_name in config.DETACHMENT_SHEETS:
        try:
            ws = await _get_worksheet(sheet_name)
            headers = await _header_map(ws)

            col_username = headers.get("username")
            if col_username is None:
                logger.warning("Sheet '%s' has no 'Username' header — skipped.", sheet_name)
                continue

            all_values = await ws.get_all_values()
            for row_idx, row in enumerate(all_values[1:], start=2):  # skip header
                cell_val = row[col_username].strip() if col_username < len(row) else ""
                if cell_val.lower() == target:
                    # Resolve column positions
                    col_lore = headers.get("lore name", headers.get("lorename"))
                    col_rank = headers.get("rank")
                    col_ep = headers.get("ep")
                    col_tp = headers.get("tp")
                    col_mp = headers.get("mp")

                    def _safe(idx: int | None, default: str = "") -> str:
                        if idx is None:
                            return default
                        return row[idx].strip() if idx < len(row) else default

                    def _safe_int(idx: int | None) -> int:
                        v = _safe(idx, "0")
                        try:
                            return int(v)
                        except ValueError:
                            return 0

                    return PlayerRow(
                        sheet_name=sheet_name,
                        row_index=row_idx,
                        username=row[col_username].strip(),  # preserve original case
                        lore_name=_safe(col_lore),
                        rank=_safe(col_rank),
                        ep=_safe_int(col_ep),
                        tp=_safe_int(col_tp),
                        mp=_safe_int(col_mp),
                        col_ep=col_ep if col_ep is not None else -1,
                        col_tp=col_tp if col_tp is not None else -1,
                        col_mp=col_mp if col_mp is not None else -1,
                    )

        except gspread.exceptions.WorksheetNotFound:
            logger.warning("Worksheet '%s' not found in spreadsheet.", sheet_name)
        except Exception:
            logger.exception("Error searching sheet '%s'.", sheet_name)

    return None


async def update_player_stats(
    player: PlayerRow, *, ep_delta: int, tp_delta: int, mp_delta: int
) -> None:
    """
    Write new EP/TP/MP totals to the player's row.
    Raises on failure so the caller knows NOT to mark the log as processed.
    """
    ws = await _get_worksheet(player.sheet_name)
    headers = await _header_map(ws)

    updates: list[gspread.Cell] = []

    def _add(col_key: str, new_val: int) -> None:
        col_idx = headers.get(col_key)
        if col_idx is not None:
            # gspread Cell is 1-indexed
            updates.append(gspread.Cell(row=player.row_index, col=col_idx + 1, value=str(new_val)))

    _add("ep", player.ep + ep_delta)
    _add("tp", player.tp + tp_delta)
    _add("mp", player.mp + mp_delta)

    if updates:
        await ws.update_cells(updates, value_input_option="RAW")
        logger.info(
            "Updated %s in [%s] row %d: EP %+d, TP %+d, MP %+d",
            player.username, player.sheet_name, player.row_index,
            ep_delta, tp_delta, mp_delta,
        )
