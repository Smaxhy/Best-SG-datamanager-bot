"""
Google Sheets service — finds players across multi-section sheets.
Only checks [D] ROOK, [D] BASTION, [C] ANCILLARY and their subdivisions.
Searches by username, lore name, or first name.
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
    ss = await _get_spreadsheet()
    try:
        return await ss.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        pass
    all_ws = await ss.worksheets()
    available_names = [ws.title for ws in all_ws]
    logger.info("Sheet '%s' not found. Available tabs: %s", sheet_name, available_names)
    target = sheet_name.strip().lower()
    for ws in all_ws:
        if ws.title.strip().lower() == target:
            logger.info("Found fuzzy match: '%s' -> '%s'", sheet_name, ws.title)
            return ws
    logger.warning("No match for '%s' in any tab.", sheet_name)
    return None


def _find_table_sections(all_values: list[list[str]]) -> list[dict]:
    sections = []
    for row_idx, row in enumerate(all_values):
        username_cols = [
            c for c, cell in enumerate(row)
            if cell.strip().upper() == "USERNAME"
        ]
        for ucol in username_cols:
            headers = {"USERNAME": ucol}
            for c in range(ucol - 1, max(-1, ucol - 4), -1):
                if 0 <= c < len(row):
                    if row[c].strip().upper() == "RANK":
                        headers["RANK"] = c
                        break
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
            logger.info("  Section: row %d, USERNAME col %d, headers=%s",
                       row_idx + 1, ucol, headers)
    return sections


def _build_player(row, headers, ws_title, row_idx) -> PlayerRow:
    def _safe(key, default=""):
        col = headers.get(key)
        if col is None or col >= len(row):
            return default
        return row[col].strip() or default

    def _safe_int(key):
        v = _safe(key, "0")
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return 0

    return PlayerRow(
        sheet_name=ws_title,
        row_index=row_idx + 1,
        username=row[headers["USERNAME"]].strip() if headers["USERNAME"] < len(row) else "",
        lore_name=_safe("LORE NAME"),
        rank=_safe("RANK"),
        ep=_safe_int("EP"),
        tp=_safe_int("TP"),
        mp=_safe_int("MP"),
        col_ep=headers.get("EP", -1),
        col_tp=headers.get("TP", -1),
        col_mp=headers.get("MP", -1),
    )


async def find_player(username: str, lore_name: str | None = None) -> PlayerRow | None:
    """
    Search priority:
    1. Exact username match (+ lore match if both given)
    2. Lore name typed as username (exact full lore match)
    3. First name match in lore column
    Log can have just username, no lore needed.
    """
    target = username.strip().lower()
    target_lore = lore_name.strip().lower() if lore_name else None
    logger.info("Searching for player '%s' (lore: %s)...", username, lore_name or "any")

    exact_user_lore: PlayerRow | None = None   # username + lore both match
    exact_user: PlayerRow | None = None         # username matches
    exact_lore_as_user: PlayerRow | None = None # full lore name matches target
    first_name: PlayerRow | None = None         # first name of lore matches target

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
                lore_col = headers.get("LORE NAME")

                for row_idx in range(header_row + 1, len(all_values)):
                    row = all_values[row_idx]
                    if username_col < len(row) and row[username_col].strip().upper() == "USERNAME":
                        break
                    if username_col >= len(row):
                        continue

                    cell_user = row[username_col].strip()
                    cell_lore = ""
                    if lore_col is not None and lore_col < len(row):
                        cell_lore = row[lore_col].strip()

                    if not cell_user and not cell_lore:
                        continue

                    cell_user_low = cell_user.lower()
                    cell_lore_low = cell_lore.lower()

                    # 1. Exact username match
                    if cell_user_low == target:
                        p = _build_player(row, headers, ws.title, row_idx)
                        # If lore also matches, best possible result
                        if target_lore and cell_lore_low == target_lore:
                            logger.info("  EXACT user+lore: '%s' / '%s' row %d", cell_user, cell_lore, row_idx+1)
                            return p
                        if exact_user is None:
                            exact_user = p
                            logger.info("  Username match: '%s' row %d, lore='%s'", cell_user, row_idx+1, cell_lore)

                    # 2. Full lore name matches what was typed as "username"
                    elif cell_lore_low and cell_lore_low == target:
                        if exact_lore_as_user is None:
                            exact_lore_as_user = _build_player(row, headers, ws.title, row_idx)
                            logger.info("  Lore-as-username: '%s' -> user '%s' row %d", target, cell_user, row_idx+1)

                    # 3. First name of lore matches
                    elif cell_lore_low:
                        lore_parts = cell_lore_low.split()
                        if lore_parts and lore_parts[0] == target:
                            if first_name is None:
                                first_name = _build_player(row, headers, ws.title, row_idx)
                                logger.info("  First-name: '%s' -> '%s' (%s) row %d", target, cell_user, cell_lore, row_idx+1)

        except Exception:
            logger.exception("Error searching sheet '%s'.", sheet_name)

    # Return best match
    for match, label in [
        (exact_user, "username"),
        (exact_lore_as_user, "lore-as-username"),
        (first_name, "first-name"),
    ]:
        if match:
            logger.info("Returning %s match: '%s' in '%s'", label, match.username, match.sheet_name)
            return match

    logger.warning("Player '%s' not found.", username)
    return None


async def update_player_stats(
    player: PlayerRow, *, ep_delta: int, tp_delta: int, mp_delta: int
) -> None:
    ws = await _get_worksheet_safe(player.sheet_name)
    if ws is None:
        raise RuntimeError(f"Cannot find sheet '{player.sheet_name}' for update")
    updates: list[gspread.Cell] = []
    if player.col_ep >= 0:
        updates.append(gspread.Cell(row=player.row_index, col=player.col_ep + 1, value=str(player.ep + ep_delta)))
    if player.col_tp >= 0:
        updates.append(gspread.Cell(row=player.row_index, col=player.col_tp + 1, value=str(player.tp + tp_delta)))
    if player.col_mp >= 0:
        updates.append(gspread.Cell(row=player.row_index, col=player.col_mp + 1, value=str(player.mp + mp_delta)))
    if updates:
        await ws.update_cells(updates, value_input_option="RAW")
        logger.info("Updated %s in [%s] row %d: EP %+d, TP %+d, MP %+d",
                    player.username, player.sheet_name, player.row_index, ep_delta, tp_delta, mp_delta)


# ── Promotion helpers ────────────────────────────────────────────────

# Maps subdivision names to their USERNAME column index in each sheet
# Left squadron = col 6, Right squadron = col 15 (based on your layout)
SUBDIVISION_MAP = {
    "[D] ROOK": {
        "Charlie": 6,
        "Delta": 15,
    },
    "[D] BASTION": {
        "Alpha": 6,
        "Bravo": 15,
    },
}


async def find_player_in_ancillary(username: str) -> PlayerRow | None:
    """Find a player specifically in the Ancillary sheet."""
    ws = await _get_worksheet_safe("[C] ANCILLARY")
    if ws is None:
        return None

    all_values = await ws.get_all_values()
    sections = _find_table_sections(all_values)
    target = username.strip().lower()

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
            if cell_val.lower() == target:
                return _build_player(row, headers, "[C] ANCILLARY", row_idx)

    return None


async def find_empty_row_in_section(sheet_name: str, subdivision: str) -> tuple[int, dict] | None:
    """
    Find the first empty row in a subdivision's section.
    Returns (1-based row index, headers dict) or None.
    """
    subs = SUBDIVISION_MAP.get(sheet_name)
    if not subs or subdivision not in subs:
        return None

    target_col = subs[subdivision]

    ws = await _get_worksheet_safe(sheet_name)
    if ws is None:
        return None

    all_values = await ws.get_all_values()
    sections = _find_table_sections(all_values)

    # Find the section with USERNAME at the target column
    for section in sections:
        if section["username_col"] != target_col:
            continue

        header_row = section["header_row"]
        headers = section["headers"]

        for row_idx in range(header_row + 1, len(all_values)):
            row = all_values[row_idx]
            # Stop at next header
            if target_col < len(row) and row[target_col].strip().upper() == "USERNAME":
                break
            # Check if username cell is empty
            cell_val = row[target_col].strip() if target_col < len(row) else ""
            if not cell_val:
                return (row_idx + 1, headers)  # 1-based

    return None


async def promote_player(
    player: PlayerRow,
    dest_sheet: str,
    subdivision: str,
    new_rank: str = "RCT",
) -> str:
    """
    Move a player from Ancillary to a destination sheet/subdivision.
    Returns a success message or raises on error.
    """
    # Find empty row in destination
    result = await find_empty_row_in_section(dest_sheet, subdivision)
    if result is None:
        raise RuntimeError(
            f"No empty row found in {dest_sheet} → {subdivision}. "
            f"The section might be full."
        )

    dest_row, dest_headers = result

    # Write to destination
    ws_dest = await _get_worksheet_safe(dest_sheet)
    if ws_dest is None:
        raise RuntimeError(f"Cannot find sheet '{dest_sheet}'")

    updates = []
    ucol = dest_headers.get("USERNAME")
    if ucol is not None:
        updates.append(gspread.Cell(row=dest_row, col=ucol + 1, value=player.username))
    lcol = dest_headers.get("LORE NAME")
    if lcol is not None:
        updates.append(gspread.Cell(row=dest_row, col=lcol + 1, value=player.lore_name))
    rcol = dest_headers.get("RANK")
    if rcol is not None:
        updates.append(gspread.Cell(row=dest_row, col=rcol + 1, value=new_rank))
    ecol = dest_headers.get("EP")
    if ecol is not None:
        updates.append(gspread.Cell(row=dest_row, col=ecol + 1, value="0"))
    tcol = dest_headers.get("TP")
    if tcol is not None:
        updates.append(gspread.Cell(row=dest_row, col=tcol + 1, value="0"))
    mcol = dest_headers.get("MP")
    if mcol is not None:
        updates.append(gspread.Cell(row=dest_row, col=mcol + 1, value="0"))

    if updates:
        await ws_dest.update_cells(updates, value_input_option="RAW")

    # Clear from Ancillary
    ws_anc = await _get_worksheet_safe("[C] ANCILLARY")
    if ws_anc is not None:
        clear_cells = []
        # Clear username, lore name, rank, EP, TP, MP
        for col in [player.col_ep, player.col_tp, player.col_mp]:
            if col >= 0:
                clear_cells.append(gspread.Cell(row=player.row_index, col=col + 1, value=""))

        # Also find and clear username and lore name columns
        all_values = await ws_anc.get_all_values()
        sections = _find_table_sections(all_values)
        for section in sections:
            if section["username_col"] < len(all_values[player.row_index - 1]):
                if all_values[player.row_index - 1][section["username_col"]].strip().lower() == player.username.lower():
                    ucol_anc = section["headers"].get("USERNAME")
                    lcol_anc = section["headers"].get("LORE NAME")
                    rcol_anc = section["headers"].get("RANK")
                    if ucol_anc is not None:
                        clear_cells.append(gspread.Cell(row=player.row_index, col=ucol_anc + 1, value=""))
                    if lcol_anc is not None:
                        clear_cells.append(gspread.Cell(row=player.row_index, col=lcol_anc + 1, value=""))
                    if rcol_anc is not None:
                        clear_cells.append(gspread.Cell(row=player.row_index, col=rcol_anc + 1, value=""))
                    break

        if clear_cells:
            await ws_anc.update_cells(clear_cells, value_input_option="RAW")

    logger.info("Promoted %s from Ancillary to %s → %s row %d",
                player.username, dest_sheet, subdivision, dest_row)

    return (
        f"**{player.username}** ({player.lore_name}) promoted to "
        f"**{dest_sheet}** → **{subdivision}** as **{new_rank}** (row {dest_row})"
    )
