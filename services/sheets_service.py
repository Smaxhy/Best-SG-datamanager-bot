"""
Google Sheets service — finds players by scanning for USERNAME headers
across multi-section sheets (multiple squadrons per tab).

Handles layouts where headers like RANK | USERNAME | LORE NAME | EP | TP | MP
appear multiple times on the same sheet at different positions.
"""

from __future__ import annotations

import json
import logging

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


async def _get_worksheet(sheet_name: str):
    client = await agcm.authorize()
    ss = await client.open_by_key(config.GOOGLE_SPREADSHEET_ID)
    return await ss.worksheet(sheet_name)


def _find_table_sections(all_values: list[list[str]]) -> list[dict]:
    """
    Scan every cell in the sheet for rows containing 'USERNAME'.
    For each match, map the surrounding headers (RANK, LORE NAME, EP, TP, MP).
    Returns a list of table sections with header positions.
    """
    sections = []

    for row_idx, row in enumerate(all_values):
        for col_idx, cell in enumerate(row):
            if cell.strip().upper() == "USERNAME":
                # Found a USERNAME header — now map the rest of the headers in this row
                header_map = {}
                for c, val in enumerate(row):
                    key = val.strip().upper()
                    if key in ("RANK", "USERNAME", "LORE NAME", "EP", "TP", "MP", "PROMO"):
                        header_map[key] = c

                if "USERNAME" in header_map:
                    sections.append({
                        "header_row": row_idx,
                        "headers": header_map,
                    })

    return sections


def _find_player_in_sections(
    all_values: list[list[str]],
    sections: list[dict],
    target: str,
) -> tuple[dict, int, list[str]] | None:
    """
    Search each section's data rows for the target username.
    Returns (section, 1-based row index, row data) or None.
    """
    target_lower = target.lower()

    for section in sections:
        header_row = section["header_row"]
        username_col = section["headers"]["USERNAME"]

        # Walk rows below the header until we hit another header, empty area, or end
        for row_idx in range(header_row + 1, len(all_values)):
            row = all_values[row_idx]

            # If this row contains "USERNAME" in any cell, we've hit the next section
            if any(c.strip().upper() == "USERNAME" for c in row):
                break

            # Get the username cell
            if username_col < len(row):
                cell_val = row[username_col].strip()
                if cell_val and cell_val.lower() == target_lower:
                    return section, row_idx + 1, row  # +1 for 1-based gspread indexing

    return None


async def find_player(username: str) -> PlayerRow | None:
    """
    Search every detachment sheet for *username* (case-insensitive).
    Handles multiple sections/squadrons per sheet.
    """
    target = username.strip()

    for sheet_name in config.DETACHMENT_SHEETS:
        try:
            ws = await _get_worksheet(sheet_name)
            all_values = await ws.get_all_values()

            sections = _find_table_sections(all_values)
            if not sections:
                logger.debug("Sheet '%s' has no USERNAME headers — skipped.", sheet_name)
                continue

            result = _find_player_in_sections(all_values, sections, target)
            if result is None:
                continue

            section, row_1based, row = result
            headers = section["headers"]

            def _safe(key: str, default: str = "") -> str:
                col = headers.get(key)
                if col is None or col >= len(row):
                    return default
                return row[col].strip() or default

            def _safe_int(key: str) -> int:
                v = _safe(key, "0")
                try:
                    return int(float(v))
                except (ValueError, TypeError):
                    return 0

            return PlayerRow(
                sheet_name=sheet_name,
                row_index=row_1based,
                username=row[headers["USERNAME"]].strip(),
                lore_name=_safe("LORE NAME"),
                rank=_safe("RANK"),
                ep=_safe_int("EP"),
                tp=_safe_int("TP"),
                mp=_safe_int("MP"),
                col_ep=headers.get("EP", -1),
                col_tp=headers.get("TP", -1),
                col_mp=headers.get("MP", -1),
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

    updates: list[gspread.Cell] = []

    if player.col_ep >= 0:
        updates.append(gspread.Cell(
            row=player.row_index, col=player.col_ep + 1,
            value=str(player.ep + ep_delta)
        ))
    if player.col_tp >= 0:
        updates.append(gspread.Cell(
            row=player.row_index, col=player.col_tp + 1,
            value=str(player.tp + tp_delta)
        ))
    if player.col_mp >= 0:
        updates.append(gspread.Cell(
            row=player.row_index, col=player.col_mp + 1,
            value=str(player.mp + mp_delta)
        ))

    if updates:
        await ws.update_cells(updates, value_input_option="RAW")
        logger.info(
            "Updated %s in [%s] row %d: EP %+d, TP %+d, MP %+d",
            player.username, player.sheet_name, player.row_index,
            ep_delta, tp_delta, mp_delta,
        )
