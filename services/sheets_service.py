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

# Rank order: index 0 = highest rank
RANK_ORDER = {"LCPL": 0, "PFC": 1, "PVT": 2, "RCT": 3}

# Maps subdivision names to their USERNAME column index in each sheet
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


def _rank_sort_key(rank_str: str) -> int:
    """Return sort key for a rank. Unknown ranks go to the bottom."""
    return RANK_ORDER.get(rank_str.strip().upper(), 99)


async def _get_section_players(ws, section: dict, all_values: list[list[str]]) -> list[dict]:
    """Read all players from a section as dicts with their row data."""
    header_row = section["header_row"]
    username_col = section["username_col"]
    headers = section["headers"]
    players = []

    for row_idx in range(header_row + 1, len(all_values)):
        row = all_values[row_idx]
        if username_col < len(row) and row[username_col].strip().upper() == "USERNAME":
            break
        if username_col >= len(row):
            continue

        cell_user = row[username_col].strip()
        if not cell_user:
            continue

        # Read all fields for this player
        data = {}
        for key, col in headers.items():
            if col < len(row):
                data[key] = row[col].strip()
            else:
                data[key] = ""

        players.append({
            "row_idx": row_idx,
            "data": data,
        })

    return players


async def _rewrite_section_sorted(ws, section: dict, all_values: list[list[str]]) -> None:
    """
    Read all players in a section, sort by rank (LCPL first, RCT last),
    then rewrite the rows in order.
    """
    header_row = section["header_row"]
    username_col = section["username_col"]
    headers = section["headers"]

    players = await _get_section_players(ws, section, all_values)
    if not players:
        return

    # Find the data row range (after header, before next header or end)
    first_data_row = header_row + 1
    last_data_row = first_data_row
    for row_idx in range(header_row + 1, len(all_values)):
        row = all_values[row_idx]
        if username_col < len(row) and row[username_col].strip().upper() == "USERNAME":
            break
        last_data_row = row_idx

    # Sort players by rank
    rank_col = headers.get("RANK")
    if rank_col is not None:
        players.sort(key=lambda p: _rank_sort_key(p["data"].get("RANK", "")))

    # Build cell updates to rewrite all rows
    updates = []
    for i, player in enumerate(players):
        row_num = first_data_row + i + 1  # 1-based
        for key, col in headers.items():
            value = player["data"].get(key, "")
            updates.append(gspread.Cell(row=row_num, col=col + 1, value=value))

    # Clear any leftover rows (if section had empty rows between players)
    for row_idx in range(first_data_row + len(players), last_data_row + 1):
        for key, col in headers.items():
            updates.append(gspread.Cell(row=row_idx + 1, col=col + 1, value=""))

    if updates:
        await ws.update_cells(updates, value_input_option="RAW")

    logger.info("Reordered section at row %d: %d players sorted by rank", header_row + 1, len(players))


async def promote_rank(player: PlayerRow, new_rank: str) -> None:
    """
    Change a player's rank in their current sheet, then reorder
    the section so higher ranks stay at the top.
    """
    ws = await _get_worksheet_safe(player.sheet_name)
    if ws is None:
        raise RuntimeError(f"Cannot find sheet '{player.sheet_name}'")

    # First update the rank cell
    all_values = await ws.get_all_values()
    sections = _find_table_sections(all_values)

    # Find which section the player is in
    target_section = None
    for section in sections:
        ucol = section["username_col"]
        header_row = section["header_row"]
        for row_idx in range(header_row + 1, len(all_values)):
            row = all_values[row_idx]
            if ucol < len(row) and row[ucol].strip().upper() == "USERNAME":
                break
            if ucol < len(row) and row[ucol].strip().lower() == player.username.lower():
                target_section = section
                # Update rank in the data
                rank_col = section["headers"].get("RANK")
                if rank_col is not None:
                    all_values[row_idx][rank_col] = new_rank
                break
        if target_section:
            break

    if target_section is None:
        raise RuntimeError(f"Player '{player.username}' not found in sheet '{player.sheet_name}'")

    # Write the rank change
    rank_col = target_section["headers"].get("RANK")
    if rank_col is not None:
        await ws.update_cell(player.row_index, rank_col + 1, new_rank)

    # Re-read and reorder the section
    all_values = await ws.get_all_values()
    await _rewrite_section_sorted(ws, target_section, all_values)

    logger.info("Promoted %s to %s in [%s], section reordered",
                player.username, new_rank, player.sheet_name)


async def find_empty_row_in_section(sheet_name: str, subdivision: str) -> tuple[int, dict] | None:
    """Find the first empty row in a subdivision's section."""
    subs = SUBDIVISION_MAP.get(sheet_name)
    if not subs or subdivision not in subs:
        return None

    target_col = subs[subdivision]
    ws = await _get_worksheet_safe(sheet_name)
    if ws is None:
        return None

    all_values = await ws.get_all_values()
    sections = _find_table_sections(all_values)

    for section in sections:
        if section["username_col"] != target_col:
            continue
        header_row = section["header_row"]
        headers = section["headers"]
        for row_idx in range(header_row + 1, len(all_values)):
            row = all_values[row_idx]
            if target_col < len(row) and row[target_col].strip().upper() == "USERNAME":
                break
            cell_val = row[target_col].strip() if target_col < len(row) else ""
            if not cell_val:
                return (row_idx + 1, headers)

    return None


async def _clear_player_row(player: PlayerRow) -> None:
    """Clear all fields for a player in their current sheet."""
    ws = await _get_worksheet_safe(player.sheet_name)
    if ws is None:
        return

    all_values = await ws.get_all_values()
    sections = _find_table_sections(all_values)
    clear_cells = []

    for section in sections:
        ucol = section["username_col"]
        if ucol < len(all_values[player.row_index - 1]):
            if all_values[player.row_index - 1][ucol].strip().lower() == player.username.lower():
                for key, col in section["headers"].items():
                    clear_cells.append(gspread.Cell(row=player.row_index, col=col + 1, value=""))
                break

    # Also clear EP/TP/MP if they're in separate columns not in headers
    for col in [player.col_ep, player.col_tp, player.col_mp]:
        if col >= 0:
            already = any(c.row == player.row_index and c.col == col + 1 for c in clear_cells)
            if not already:
                clear_cells.append(gspread.Cell(row=player.row_index, col=col + 1, value=""))

    if clear_cells:
        await ws.update_cells(clear_cells, value_input_option="RAW")


async def transfer_player(
    player: PlayerRow,
    dest_sheet: str,
    subdivision: str,
    new_rank: str = "RCT",
) -> str:
    """
    Move a player from their current sheet to a new detachment/subdivision.
    Clears old row, writes to new sheet, reorders by rank.
    """
    result = await find_empty_row_in_section(dest_sheet, subdivision)
    if result is None:
        raise RuntimeError(
            f"No empty row found in {dest_sheet} → {subdivision}. Section might be full."
        )

    dest_row, dest_headers = result

    # Write to destination
    ws_dest = await _get_worksheet_safe(dest_sheet)
    if ws_dest is None:
        raise RuntimeError(f"Cannot find sheet '{dest_sheet}'")

    updates = []
    for key, value in [
        ("USERNAME", player.username),
        ("LORE NAME", player.lore_name),
        ("RANK", new_rank),
        ("EP", "0"),
        ("TP", "0"),
        ("MP", "0"),
    ]:
        col = dest_headers.get(key)
        if col is not None:
            updates.append(gspread.Cell(row=dest_row, col=col + 1, value=value))

    if updates:
        await ws_dest.update_cells(updates, value_input_option="RAW")

    # Clear from old sheet
    await _clear_player_row(player)

    # Reorder destination section by rank
    all_values = await ws_dest.get_all_values()
    sections = _find_table_sections(all_values)
    target_col = SUBDIVISION_MAP.get(dest_sheet, {}).get(subdivision)
    for section in sections:
        if section["username_col"] == target_col:
            await _rewrite_section_sorted(ws_dest, section, all_values)
            break

    logger.info("Transferred %s from %s to %s → %s row %d",
                player.username, player.sheet_name, dest_sheet, subdivision, dest_row)

    return (
        f"**{player.username}** ({player.lore_name})\n"
        f"From: **{player.sheet_name}**\n"
        f"To: **{dest_sheet}** → **{subdivision}** as **{new_rank}**\n"
        f"EP/TP/MP reset to 0"
    )
