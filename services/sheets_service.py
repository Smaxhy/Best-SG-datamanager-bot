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
                elif v == "EXAM" and "EXAM" not in headers:
                    headers["EXAM"] = c
                elif v == "SCOUTED" and "SCOUTED" not in headers:
                    headers["SCOUTED"] = c
                elif v == "DEVICE STATUS" and "DEVICE STATUS" not in headers:
                    headers["DEVICE STATUS"] = c
                elif v == "LAST INSPECTED" and "LAST INSPECTED" not in headers:
                    headers["LAST INSPECTED"] = c
                elif v == "SOURCED BY" and "SOURCED BY" not in headers:
                    headers["SOURCED BY"] = c
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
                            # Prefer match with stat columns
                            if p.col_ep >= 0:
                                return p
                            if exact_user is None:
                                exact_user = p
                        if exact_user is None:
                            exact_user = p
                            logger.info("  Username match: '%s' row %d, lore='%s'", cell_user, row_idx+1, cell_lore)
                        elif p.col_ep >= 0 and exact_user.col_ep < 0:
                            # Upgrade to a match that has stat columns
                            exact_user = p
                            logger.info("  Upgraded username match to section with stats: '%s' row %d", cell_user, row_idx+1)

                    # 2. Full lore name matches what was typed as "username"
                    elif cell_lore_low and cell_lore_low == target:
                        if exact_lore_as_user is None:
                            exact_lore_as_user = _build_player(row, headers, ws.title, row_idx)
                            logger.info("  Lore-as-username: '%s' -> user '%s' row %d", target, cell_user, row_idx+1)
                        else:
                            p = _build_player(row, headers, ws.title, row_idx)
                            if p.col_ep >= 0 and exact_lore_as_user.col_ep < 0:
                                exact_lore_as_user = p
                                logger.info("  Upgraded lore match to section with stats: '%s' row %d", target, row_idx+1)

                    # 3. First name of lore matches
                    elif cell_lore_low:
                        lore_parts = cell_lore_low.split()
                        if lore_parts and lore_parts[0] == target:
                            if first_name is None:
                                first_name = _build_player(row, headers, ws.title, row_idx)
                                logger.info("  First-name: '%s' -> '%s' (%s) row %d", target, cell_user, cell_lore, row_idx+1)
                            else:
                                p = _build_player(row, headers, ws.title, row_idx)
                                if p.col_ep >= 0 and first_name.col_ep < 0:
                                    first_name = p

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
    else:
        logger.warning("No stat columns for %s in [%s] row %d — EP/TP/MP not updated!",
                       player.username, player.sheet_name, player.row_index)
        raise RuntimeError(
            f"**{player.username}** found in **{player.sheet_name}** row {player.row_index} "
            f"but that section has no EP/TP/MP columns — points not added."
        )


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
    "[U] VALKYRIE": {
        "Aether": 6,
        "Helios": 15,
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


# Promotion requirements: what you need to go FROM current rank TO next rank
# RCT → PVT: 4 EP + 1 TP + Scouted + Exam (scouted/exam checked manually)
# PVT → PFC: 9 EP + 4 TP
# PFC → LCPL: 10 EP + 6 TP + 2 MP
RANK_REQUIREMENTS = {
    "PVT":  {"from": "RCT",  "ep": 4,  "tp": 1, "mp": 0, "extra": "Scouted + Exam (verify manually)"},
    "PFC":  {"from": "PVT",  "ep": 9,  "tp": 4, "mp": 0, "extra": None},
    "LCPL": {"from": "PFC",  "ep": 10, "tp": 6, "mp": 2, "extra": None},
}


def check_promotion_requirements(player: PlayerRow, new_rank: str) -> tuple[bool, str]:
    """
    Check if a player meets the requirements for promotion.
    Returns (eligible: bool, message: str).
    If eligible, message shows what points will remain after deduction.
    """
    new_rank_upper = new_rank.strip().upper()
    req = RANK_REQUIREMENTS.get(new_rank_upper)

    if req is None:
        return True, "No requirements defined for this rank."

    # Check if promoting from the correct rank
    current = player.rank.strip().upper()
    if current != req["from"]:
        # Allow skipping ranks (e.g. RCT → PFC) by checking all intermediate reqs
        # But for now, just warn
        pass

    # Check points
    missing = []
    if player.ep < req["ep"]:
        missing.append(f"EP: has {player.ep}, needs {req['ep']}")
    if player.tp < req["tp"]:
        missing.append(f"TP: has {player.tp}, needs {req['tp']}")
    if req["mp"] > 0 and player.mp < req["mp"]:
        missing.append(f"MP: has {player.mp}, needs {req['mp']}")

    if missing:
        msg = (
            f"❌ **{player.username}** does not meet requirements for **{new_rank_upper}**:\n"
            + "\n".join(f"  • {m}" for m in missing)
        )
        if req["extra"]:
            msg += f"\n  • Also requires: {req['extra']}"
        return False, msg

    # Eligible — calculate remaining points
    remaining_ep = player.ep - req["ep"]
    remaining_tp = player.tp - req["tp"]
    remaining_mp = player.mp - req["mp"]

    msg = (
        f"✅ Requirements met for **{new_rank_upper}**\n"
        f"EP: {player.ep} - {req['ep']} = **{remaining_ep}** remaining\n"
        f"TP: {player.tp} - {req['tp']} = **{remaining_tp}** remaining"
    )
    if req["mp"] > 0:
        msg += f"\nMP: {player.mp} - {req['mp']} = **{remaining_mp}** remaining"
    if req["extra"]:
        msg += f"\n⚠️ Also requires: {req['extra']}"

    return True, msg


async def promote_rank(player: PlayerRow, new_rank: str, force: bool = False) -> str:
    """
    Change a player's rank in their current sheet.
    Checks requirements, deducts points, then reorders the section.
    Returns a summary message.
    """
    new_rank_upper = new_rank.strip().upper()

    # Check requirements
    eligible, req_msg = check_promotion_requirements(player, new_rank_upper)
    if not eligible and not force:
        raise ValueError(req_msg)

    ws = await _get_worksheet_safe(player.sheet_name)
    if ws is None:
        raise RuntimeError(f"Cannot find sheet '{player.sheet_name}'")

    all_values = await ws.get_all_values()
    sections = _find_table_sections(all_values)

    # Find which section the player is in
    target_section = None
    player_row_idx = None
    for section in sections:
        ucol = section["username_col"]
        header_row = section["header_row"]
        for row_idx in range(header_row + 1, len(all_values)):
            row = all_values[row_idx]
            if ucol < len(row) and row[ucol].strip().upper() == "USERNAME":
                break
            if ucol < len(row) and row[ucol].strip().lower() == player.username.lower():
                target_section = section
                player_row_idx = row_idx
                break
        if target_section:
            break

    if target_section is None:
        raise RuntimeError(f"Player '{player.username}' not found in sheet '{player.sheet_name}'")

    headers = target_section["headers"]

    # Calculate new point values after deduction
    req = RANK_REQUIREMENTS.get(new_rank_upper)
    if req and not force:
        new_ep = player.ep - req["ep"]
        new_tp = player.tp - req["tp"]
        new_mp = player.mp - req["mp"]
    else:
        new_ep = player.ep
        new_tp = player.tp
        new_mp = player.mp

    # Write rank + deducted points
    updates = []
    rank_col = headers.get("RANK")
    if rank_col is not None:
        updates.append(gspread.Cell(row=player.row_index, col=rank_col + 1, value=new_rank_upper))
    ep_col = headers.get("EP")
    if ep_col is not None:
        updates.append(gspread.Cell(row=player.row_index, col=ep_col + 1, value=str(new_ep)))
    tp_col = headers.get("TP")
    if tp_col is not None:
        updates.append(gspread.Cell(row=player.row_index, col=tp_col + 1, value=str(new_tp)))
    mp_col = headers.get("MP")
    if mp_col is not None and req and req["mp"] > 0:
        updates.append(gspread.Cell(row=player.row_index, col=mp_col + 1, value=str(new_mp)))

    if updates:
        await ws.update_cells(updates, value_input_option="RAW")

    # Also update the in-memory data for reordering
    if player_row_idx is not None:
        if rank_col is not None:
            all_values[player_row_idx][rank_col] = new_rank_upper
        if ep_col is not None:
            all_values[player_row_idx][ep_col] = str(new_ep)
        if tp_col is not None:
            all_values[player_row_idx][tp_col] = str(new_tp)

    # Re-read and reorder the section
    all_values = await ws.get_all_values()
    await _rewrite_section_sorted(ws, target_section, all_values)

    logger.info("Promoted %s to %s in [%s], points deducted, section reordered",
                player.username, new_rank_upper, player.sheet_name)

    result = req_msg + f"\n\nRank: **{player.rank}** → **{new_rank_upper}**"
    return result


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


def _is_ancillary(sheet_name: str) -> bool:
    """Check if a sheet is the Ancillary sheet (fuzzy)."""
    return "ANCILLARY" in sheet_name.upper()


def _is_rook_or_bastion(sheet_name: str) -> bool:
    return "ROOK" in sheet_name.upper() or "BASTION" in sheet_name.upper()


async def transfer_player(
    player: PlayerRow,
    dest_sheet: str,
    subdivision: str,
    new_rank: str = "RCT",
) -> dict:
    """
    Move a player from their current sheet to a new detachment/subdivision.

    Two modes:
    - Ancillary → Rook/Bastion: ranked PROMOTION — checks requirements,
      deducts points, keeps remainder.
    - Rook ↔ Bastion: lateral TRANSFER — keeps all points as-is.

    Returns a dict with: message, mode ("promotion"|"transfer"), old_ep/tp/mp,
    new_ep/tp/mp, deducted_ep/tp/mp, old_rank, new_rank.
    """
    from_ancillary = _is_ancillary(player.sheet_name)
    to_rook_bastion = _is_rook_or_bastion(dest_sheet)
    lateral = _is_rook_or_bastion(player.sheet_name) and to_rook_bastion

    # Determine point handling
    if from_ancillary and to_rook_bastion:
        mode = "promotion"
        # Check promotion requirements
        new_rank_upper = new_rank.strip().upper()
        req = RANK_REQUIREMENTS.get(new_rank_upper)
        if req:
            eligible, req_msg = check_promotion_requirements(player, new_rank_upper)
            if not eligible:
                raise ValueError(req_msg)
            dest_ep = player.ep - req["ep"]
            dest_tp = player.tp - req["tp"]
            dest_mp = player.mp - req["mp"]
        else:
            # No requirements for this rank (e.g. RCT) — keep all points
            dest_ep = player.ep
            dest_tp = player.tp
            dest_mp = player.mp
    elif lateral:
        mode = "transfer"
        # Carry all points across
        dest_ep = player.ep
        dest_tp = player.tp
        dest_mp = player.mp
    else:
        # Fallback (e.g. Rook/Bastion → Ancillary, or any other combo) — reset
        mode = "transfer"
        dest_ep = 0
        dest_tp = 0
        dest_mp = 0

    # Find empty row in destination
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
        ("RANK", new_rank.strip().upper()),
        ("EP", str(dest_ep)),
        ("TP", str(dest_tp)),
        ("MP", str(dest_mp)),
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

    old_rank = player.rank
    logger.info("Transferred %s from %s to %s → %s (mode=%s)",
                player.username, player.sheet_name, dest_sheet, subdivision, mode)

    # Build result dict for the command to use
    req_info = RANK_REQUIREMENTS.get(new_rank.strip().upper())
    deducted_ep = req_info["ep"] if (mode == "promotion" and req_info) else 0
    deducted_tp = req_info["tp"] if (mode == "promotion" and req_info) else 0
    deducted_mp = req_info["mp"] if (mode == "promotion" and req_info) else 0

    if mode == "promotion":
        msg = (
            f"**{player.username}** ({player.lore_name})\n"
            f"From: **{player.sheet_name}**\n"
            f"To: **{dest_sheet}** → **{subdivision}** as **{new_rank.upper()}**\n"
            f"Rank: **{old_rank}** → **{new_rank.upper()}**\n\n"
            f"**Point Conversion:**\n"
            f"EP: {player.ep} - {deducted_ep} = **{dest_ep}** remaining\n"
            f"TP: {player.tp} - {deducted_tp} = **{dest_tp}** remaining\n"
            f"MP: {player.mp} - {deducted_mp} = **{dest_mp}** remaining"
        )
    elif lateral:
        msg = (
            f"**{player.username}** ({player.lore_name})\n"
            f"From: **{player.sheet_name}**\n"
            f"To: **{dest_sheet}** → **{subdivision}** as **{new_rank.upper()}**\n\n"
            f"**Points carried over:**\n"
            f"EP: **{dest_ep}** | TP: **{dest_tp}** | MP: **{dest_mp}**"
        )
    else:
        msg = (
            f"**{player.username}** ({player.lore_name})\n"
            f"From: **{player.sheet_name}**\n"
            f"To: **{dest_sheet}** → **{subdivision}** as **{new_rank.upper()}**\n"
            f"EP/TP/MP reset to 0"
        )

    return {
        "message": msg,
        "mode": mode,
        "old_rank": old_rank,
        "new_rank": new_rank.strip().upper(),
        "old_ep": player.ep, "old_tp": player.tp, "old_mp": player.mp,
        "new_ep": dest_ep, "new_tp": dest_tp, "new_mp": dest_mp,
        "deducted_ep": deducted_ep, "deducted_tp": deducted_tp, "deducted_mp": deducted_mp,
    }


# ── Deathlog: find and remove player from ALL sheets ────────────────

async def find_all_player_rows(username: str) -> list[PlayerRow]:
    """Find a player across ALL sheets — returns every row they appear in."""
    target = username.strip().lower()
    found: list[PlayerRow] = []

    for sheet_name in config.DETACHMENT_SHEETS:
        try:
            ws = await _get_worksheet_safe(sheet_name)
            if ws is None:
                continue
            all_values = await ws.get_all_values()
            sections = _find_table_sections(all_values)

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

                    cell_user = row[username_col].strip().lower()
                    cell_lore = ""
                    if lore_col is not None and lore_col < len(row):
                        cell_lore = row[lore_col].strip().lower()

                    # Match by username, full lore name, or first name
                    matched = False
                    if cell_user == target:
                        matched = True
                    elif cell_lore and cell_lore == target:
                        matched = True
                    elif cell_lore:
                        parts = cell_lore.split()
                        if parts and parts[0] == target:
                            matched = True

                    if matched and cell_user:
                        found.append(_build_player(row, headers, ws.title, row_idx))

        except Exception:
            logger.exception("Error searching sheet '%s' for deathlog.", sheet_name)

    return found


async def remove_player_rows(players: list[PlayerRow]) -> int:
    """Clear all given player rows from their sheets. Returns count removed."""
    removed = 0
    for player in players:
        try:
            await _clear_player_row(player)
            removed += 1
            logger.info("Deathlog: removed %s from %s row %d",
                        player.username, player.sheet_name, player.row_index)
        except Exception:
            logger.exception("Failed to remove %s from %s", player.username, player.sheet_name)
    return removed


# ── Examcheck: set EXAM checkbox in Ancillary ──────────────────────

async def set_exam_checkmark(username: str) -> bool:
    """
    Find a player in the Ancillary sheet and set their EXAM checkbox to TRUE.
    Returns True if found and updated.
    """
    for sheet_name in config.DETACHMENT_SHEETS:
        if not _is_ancillary(sheet_name):
            continue

        ws = await _get_worksheet_safe(sheet_name)
        if ws is None:
            continue

        all_values = await ws.get_all_values()
        sections = _find_table_sections(all_values)
        target = username.strip().lower()

        for section in sections:
            header_row = section["header_row"]
            username_col = section["username_col"]
            headers = section["headers"]
            exam_col = headers.get("EXAM")
            if exam_col is None:
                logger.warning("No EXAM column found in section at row %d of %s",
                               header_row + 1, sheet_name)
                continue

            for row_idx in range(header_row + 1, len(all_values)):
                row = all_values[row_idx]
                if username_col < len(row) and row[username_col].strip().upper() == "USERNAME":
                    break
                if username_col >= len(row):
                    continue

                cell_user = row[username_col].strip().lower()
                if cell_user == target:
                    # Set the EXAM checkbox to TRUE
                    await ws.update_cell(row_idx + 1, exam_col + 1, True)
                    logger.info("Set EXAM checkmark for %s in %s row %d",
                                username, sheet_name, row_idx + 1)
                    return True

    return False


async def find_ancillary_player(username: str) -> PlayerRow | None:
    """Find a player specifically in the Ancillary sheet."""
    target = username.strip().lower()
    for sheet_name in config.DETACHMENT_SHEETS:
        if not _is_ancillary(sheet_name):
            continue

        ws = await _get_worksheet_safe(sheet_name)
        if ws is None:
            continue

        all_values = await ws.get_all_values()
        sections = _find_table_sections(all_values)

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

                cell_user = row[username_col].strip().lower()
                lore_col = headers.get("LORE NAME")
                cell_lore = ""
                if lore_col is not None and lore_col < len(row):
                    cell_lore = row[lore_col].strip().lower()

                if cell_user == target or cell_lore == target:
                    return _build_player(row, headers, ws.title, row_idx)

    return None


# ── Add player to any sheet ─────────────────────────────────────────

async def add_player_to_sheet(
    username: str,
    lore_name: str,
    sheet_name: str,
    subdivision: str | None = None,
    extra_fields: dict | None = None,
) -> str:
    """
    Add a new player to a sheet. For Rook/Bastion, subdivision is required.
    For other sheets (Ancillary, Medical, etc.), finds the first section with
    a USERNAME column that has stat columns and inserts there.

    Returns a success message string.
    """
    ws = await _get_worksheet_safe(sheet_name)
    if ws is None:
        raise RuntimeError(f"Cannot find sheet '{sheet_name}'")

    all_values = await ws.get_all_values()
    sections = _find_table_sections(all_values)

    if not sections:
        raise RuntimeError(f"No data sections found in '{sheet_name}'")

    # If subdivision is given (Rook/Bastion), use the existing function
    if subdivision and sheet_name in SUBDIVISION_MAP:
        result = await find_empty_row_in_section(sheet_name, subdivision)
        if result is None:
            raise RuntimeError(f"No empty row in {sheet_name} → {subdivision}")
        dest_row, headers = result
    else:
        # Find the first section with EP columns and an empty row
        dest_row = None
        headers = None
        for section in sections:
            if "EP" not in section["headers"]:
                continue  # skip command sections
            header_row = section["header_row"]
            username_col = section["username_col"]
            h = section["headers"]
            for row_idx in range(header_row + 1, len(all_values)):
                row = all_values[row_idx]
                if username_col < len(row) and row[username_col].strip().upper() == "USERNAME":
                    break
                cell_val = row[username_col].strip() if username_col < len(row) else ""
                if not cell_val:
                    dest_row = row_idx + 1
                    headers = h
                    break
            if dest_row:
                break

        if dest_row is None or headers is None:
            raise RuntimeError(f"No empty row found in '{sheet_name}'")

    # Write the player — build fields based on what the section has
    fields = [
        ("USERNAME", username),
        ("LORE NAME", lore_name),
    ]

    # Only add RANK if the section has it
    if "RANK" in headers:
        fields.append(("RANK", "RCT"))

    # Stat columns (Rook/Bastion/Ancillary)
    for stat in ("EP", "TP", "MP"):
        if stat in headers:
            fields.append((stat, "0"))

    # Apply any extra fields passed from the command (e.g. Device Wielder fields)
    if extra_fields:
        for key, value in extra_fields.items():
            if key in headers:
                fields.append((key, value))

    updates = []
    for key, value in fields:
        col = headers.get(key)
        if col is not None:
            updates.append(gspread.Cell(row=dest_row, col=col + 1, value=value))

    if updates:
        await ws.update_cells(updates, value_input_option="RAW")

    location = f"{sheet_name} → {subdivision}" if subdivision else sheet_name
    logger.info("Added player %s (%s) to %s row %d", username, lore_name, location, dest_row)

    return (
        f"**{username}** ({lore_name}) added to **{location}** as **RCT** "
        f"(row {dest_row})"
    )
