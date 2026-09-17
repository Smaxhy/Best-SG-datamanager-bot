"""
Google Sheets service — finds players by scanning entire sheets
for USERNAME headers, handling multi-section layouts.
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


async def find_player(username: str) -> PlayerRow | None:
    """
    Search every detachment sheet for *username* (case-insensitive).
    
    Strategy:
    1. Get all values from the sheet
    2. Find every row that contains a "USERNAME" header cell
    3. For each such header row, map surrounding columns (RANK, LORE NAME, EP, TP, MP)
    4. Search rows below each header for the target username
    """
    target = username.strip().lower()
    logger.info("Searching for player '%s' across %d sheets...", username, len(config.DETACHMENT_SHEETS))

    for sheet_name in config.DETACHMENT_SHEETS:
        try:
            ws = await _get_worksheet(sheet_name)
            all_values = await ws.get_all_values()
            logger.info("Sheet '%s': loaded %d rows, %d cols", 
                       sheet_name, len(all_values), 
                       len(all_values[0]) if all_values else 0)

            # Step 1: Find ALL header rows (rows containing "USERNAME" in any cell)
            header_sections = []
            for row_idx, row in enumerate(all_values):
                for col_idx, cell in enumerate(row):
                    cleaned = cell.strip().upper()
                    if cleaned == "USERNAME":
                        # Map all headers in this row
                        headers = {}
                        for c, val in enumerate(row):
                            v = val.strip().upper()
                            if v == "RANK":
                                headers["RANK"] = c
                            elif v == "USERNAME":
                                headers["USERNAME"] = c
                            elif v in ("LORE NAME", "LORENAME", "LORE"):
                                headers["LORE NAME"] = c
                            elif v == "EP":
                                headers["EP"] = c
                            elif v == "TP":
                                headers["TP"] = c
                            elif v == "MP":
                                headers["MP"] = c
                        
                        header_sections.append({
                            "header_row": row_idx,
                            "username_col": col_idx,
                            "headers": headers,
                        })
                        logger.info("  Found USERNAME header at row %d, col %d. Headers: %s", 
                                   row_idx, col_idx, headers)
                        break  # only count first USERNAME per row

            if not header_sections:
                logger.info("  No USERNAME headers found in '%s'", sheet_name)
                continue

            # Step 2: Search for the player in rows below each header
            for section in header_sections:
                header_row = section["header_row"]
                username_col = section["username_col"]
                headers = section["headers"]

                for row_idx in range(header_row + 1, len(all_values)):
                    row = all_values[row_idx]
                    
                    # Stop if we hit another USERNAME header
                    if any(c.strip().upper() == "USERNAME" for c in row):
                        break

                    # Check this row
                    if username_col < len(row):
                        cell_val = row[username_col].strip()
                        if cell_val.lower() == target:
                            logger.info("  FOUND '%s' at row %d in section starting at row %d",
                                       cell_val, row_idx + 1, header_row + 1)

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
                                row_index=row_idx + 1,  # 1-based for gspread
                                username=cell_val,
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

    logger.warning("Player '%s' not found in any sheet.", username)
    return None


async def update_player_stats(
    player: PlayerRow, *, ep_delta: int, tp_delta: int, mp_delta: int
) -> None:
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
