"""
Google Sheets service — finds players across multi-section sheets.
Only checks [D] ROOK, [D] BASTION, [C] ANCILLARY and their subdivisions.
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


async def _get_spreadsheet():
    client = await agcm.authorize()
    return await client.open_by_key(config.GOOGLE_SPREADSHEET_ID)


async def _get_worksheet_safe(sheet_name: str):
    """
    Try to find the worksheet by exact name first.
    If not found, list all worksheets and try case-insensitive / stripped match.
    """
    ss = await _get_spreadsheet()
    
    # Try exact match first
    try:
        return await ss.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        pass
    
    # List all worksheets and try fuzzy match
    all_ws = await ss.worksheets()
    available_names = [ws.title for ws in all_ws]
    logger.info("Sheet '%s' not found. Available tabs: %s", sheet_name, available_names)
    
    target = sheet_name.strip().lower()
    for ws in all_ws:
        if ws.title.strip().lower() == target:
            logger.info("Found fuzzy match: '%s' → '%s'", sheet_name, ws.title)
            return ws
    
    logger.warning("No match for '%s' in any tab.", sheet_name)
    return None


def _find_table_sections(all_values: list[list[str]]) -> list[dict]:
    """
    Find every USERNAME header in the sheet.
    For each one:
      - Look LEFT (max 3 cols) for RANK
      - Look RIGHT (max 8 cols) for LORE NAME, EP, TP, MP
      - Stop looking right if we hit another USERNAME
    """
    sections = []

    for row_idx, row in enumerate(all_values):
        username_cols = [
            c for c, cell in enumerate(row)
            if cell.strip().upper() == "USERNAME"
        ]

        for ucol in username_cols:
            headers = {"USERNAME": ucol}

            # Look LEFT for RANK (max 3 cols)
            for c in range(ucol - 1, max(-1, ucol - 4), -1):
                if c >= 0 and c < len(row):
                    v = row[c].strip().upper()
                    if v == "RANK":
                        headers["RANK"] = c
                        break

            # Look RIGHT for LORE NAME, EP, TP, MP (max 8 cols)
            # Stop if we hit another USERNAME
            for c in range(ucol + 1, min(len(row), ucol + 9)):
                v = row[c].strip().upper()
                if v == "USERNAME":
                    break
                if v in ("LORE NAME", "LORENAME", "LORE") and "LORE NAME" not in headers:
                    headers["LORE NAME"] = c
                elif v == "EP" and "EP" not in headers:
                    headers["EP"] = c
                elif v == "TP" and "TP" not in headers:
                    headers["TP"] = c
                elif v == "MP" and "MP" not in headers:
                    headers["MP"] = c

            sections.append({
                "header_row": row_idx,
                "username_col": ucol,
                "headers": headers,
            })
            logger.info(
                "  Section: row %d, USERNAME col %d, headers=%s",
                row_idx + 1, ucol, headers
            )

    return sections


async def find_player(username: str, lore_name: str | None = None) -> PlayerRow | None:
    target = username.strip().lower()
    target_lore = lore_name.strip().lower() if lore_name else None
    logger.info("Searching for player '%s' (lore: %s) across sheets: %s",
                username, lore_name or "any", config.DETACHMENT_SHEETS)

    best_match: PlayerRow | None = None

    for sheet_name in config.DETACHMENT_SHEETS:
        try:
            ws = await _get_worksheet_safe(sheet_name)
            if ws is None:
                continue
                
            all_values = await ws.get_all_values()
            logger.info("Sheet '%s': %d rows", ws.title, len(all_values))

            sections = _find_table_sections(all_values)
            if not sections:
                continue

            for section in sections:
                header_row = section["header_row"]
                username_col = section["username_col"]
                headers = section["headers"]

                for row_idx in range(header_row + 1, len(all_values)):
                    row = all_values[row_idx]

                    if username_col < len(row) and row[username_col].strip().upper() == "USERNAME":
                        break

                    if username_col >= len(row):
                        continue

                    cell_val = row[username_col].strip()
                    if cell_val.lower() != target:
                        continue

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

                    found_lore = _safe("LORE NAME")
                    player = PlayerRow(
                        sheet_name=ws.title,  # use actual sheet title
                        row_index=row_idx + 1,
                        username=cell_val,
                        lore_name=found_lore,
                        rank=_safe("RANK"),
                        ep=_safe_int("EP"),
                        tp=_safe_int("TP"),
                        mp=_safe_int("MP"),
                        col_ep=headers.get("EP", -1),
                        col_tp=headers.get("TP", -1),
                        col_mp=headers.get("MP", -1),
                    )

                    logger.info(
                        "  Match: '%s' row %d, sheet '%s', lore='%s', "
                        "EP_col=%s, TP_col=%s, MP_col=%s",
                        cell_val, row_idx + 1, ws.title, found_lore,
                        headers.get("EP"), headers.get("TP"), headers.get("MP")
                    )

                    if target_lore and found_lore.lower() == target_lore:
                        logger.info("  → Exact lore match!")
                        return player

                    if best_match is None:
                        best_match = player

        except Exception:
            logger.exception("Error searching sheet '%s'.", sheet_name)

    if best_match:
        logger.info("Returning best match: '%s' in '%s'", best_match.username, best_match.sheet_name)
        return best_match

    logger.warning("Player '%s' not found in any sheet.", username)
    return None


async def update_player_stats(
    player: PlayerRow, *, ep_delta: int, tp_delta: int, mp_delta: int
) -> None:
    ws = await _get_worksheet_safe(player.sheet_name)
    if ws is None:
        raise RuntimeError(f"Cannot find sheet '{player.sheet_name}' for update")
    
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
