"""
Google Sheets service — finds players across multi-section sheets.
Only checks [D] ROOK, [D] BASTION, [C] ANCILLARY and their subdivisions.
Searches by username, lore name, or first name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random

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


class _BackoffClientManager(AsyncioGspreadClientManager):
    """
    gspread_asyncio retries rate-limit (429) and server errors forever, every
    1.1s, while holding the lock every other Sheets call waits on. That makes
    one bad patch freeze the whole bot. Back off exponentially instead and give
    up after a few tries so the command can report the error.
    """

    MAX_RETRIES = 6
    MAX_BACKOFF = 60.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._failures = 0

    async def _call(self, method, *args, **kwargs):
        try:
            return await super()._call(method, *args, **kwargs)
        finally:
            # Calls run one at a time, so this only resets the call that just finished
            self._failures = 0

    async def _backoff(self, e, method) -> None:
        self._failures += 1
        if self._failures > self.MAX_RETRIES:
            self._failures = 0
            logger.error("Google Sheets call %s failed %d times, giving up: %s",
                         method.__name__, self.MAX_RETRIES + 1, e)
            raise e
        wait = min(self.MAX_BACKOFF, 2 ** self._failures) + random.uniform(0, 1)
        logger.warning("Google Sheets call %s failed (%s), retry %d/%d in %.1fs",
                       method.__name__, e, self._failures, self.MAX_RETRIES, wait)
        await asyncio.sleep(wait)

    async def handle_gspread_error(self, e, method, args, kwargs):
        await self._backoff(e, method)

    async def handle_requests_error(self, e, method, args, kwargs):
        await self._backoff(e, method)


agcm = _BackoffClientManager(_get_credentials)

# Held while reading-then-writing a sheet, so two commands can't interleave
# their read and write and overwrite each other's changes.
_write_lock = asyncio.Lock()


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


class SheetCache:
    """
    Remembers each tab's values for the length of one command, so looking up
    many players costs one API read per tab instead of one per player.
    Only used for finding players; every write re-reads the sheet first.
    """

    def __init__(self) -> None:
        self._tabs: dict[str, tuple] = {}
        self._errors: dict[str, Exception] = {}

    async def read(self, sheet_name: str):
        """
        Return (worksheet, all_values), or (None, None) if the tab doesn't exist.
        A failed read is remembered and re-raised, so a broken tab isn't retried
        (with back-off) for every single player.
        """
        if sheet_name in self._errors:
            raise self._errors[sheet_name]
        if sheet_name not in self._tabs:
            try:
                ws = await _get_worksheet_safe(sheet_name)
                values = await ws.get_all_values() if ws is not None else None
            except Exception as exc:
                self._errors[sheet_name] = exc
                raise
            self._tabs[sheet_name] = (ws, values)
        return self._tabs[sheet_name]

    async def preload(self) -> dict[str, Exception]:
        """Read every player tab now. Returns {tab: error} for tabs that failed."""
        for sheet_name in config.DETACHMENT_SHEETS:
            try:
                await self.read(sheet_name)
            except Exception:
                logger.exception("Could not read sheet '%s'.", sheet_name)
        return dict(self._errors)


async def _read_sheet(sheet_name: str, cache: SheetCache | None):
    if cache is not None:
        return await cache.read(sheet_name)
    ws = await _get_worksheet_safe(sheet_name)
    if ws is None:
        return None, None
    return ws, await ws.get_all_values()


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


async def find_player(
    username: str, lore_name: str | None = None, cache: SheetCache | None = None
) -> PlayerRow | None:
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
            ws, all_values = await _read_sheet(sheet_name, cache)
            if ws is None:
                continue
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


def _section_rows(all_values: list[list[str]], section: dict):
    """Yield (row_idx, row) for the data rows under a section header."""
    ucol = section["username_col"]
    for row_idx in range(section["header_row"] + 1, len(all_values)):
        row = all_values[row_idx]
        if ucol < len(row) and row[ucol].strip().upper() == "USERNAME":
            break
        yield row_idx, row


def _locate_player(all_values: list[list[str]], player: PlayerRow) -> tuple[int, dict] | None:
    """
    Find *player*'s row in freshly read sheet values. Rows move when sections
    get re-sorted or players are transferred, so don't trust row_index blindly.
    Returns (0-based row index, section) or None if they're gone.
    """
    target = player.username.strip().lower()
    candidates = []
    for section in _find_table_sections(all_values):
        ucol = section["username_col"]
        for row_idx, row in _section_rows(all_values, section):
            if ucol < len(row) and row[ucol].strip().lower() == target:
                candidates.append((row_idx, section))
    if not candidates:
        return None

    def score(c):
        row_idx, section = c
        headers = section["headers"]
        same_cols = headers.get("EP", -1) == player.col_ep
        return (
            row_idx == player.row_index - 1 and same_cols,
            same_cols,
            "EP" in headers,
        )

    return max(candidates, key=score)


def _no_stat_columns_error(player: PlayerRow) -> RuntimeError:
    return RuntimeError(
        f"**{player.username}** found in **{player.sheet_name}** row {player.row_index} "
        f"but that section has no EP/TP/MP columns — points not added."
    )


async def apply_stat_changes(
    changes: list[tuple[PlayerRow, int, int, int]],
) -> list[Exception | None]:
    """
    Add (ep, tp, mp) deltas for many players at once — one fresh read and one
    write per tab. Totals are computed from the values in the sheet *now*, not
    when the player was looked up, so edits made in the meantime aren't lost.

    Returns one entry per change: None on success, or the error for that player.
    """
    results: list[Exception | None] = [None] * len(changes)
    by_sheet: dict[str, list[int]] = {}
    for i, (player, *_deltas) in enumerate(changes):
        by_sheet.setdefault(player.sheet_name, []).append(i)

    async with _write_lock:
        for sheet_name, idxs in by_sheet.items():
            try:
                ws = await _get_worksheet_safe(sheet_name)
                if ws is None:
                    raise RuntimeError(f"Cannot find sheet '{sheet_name}' for update")
                all_values = await ws.get_all_values()
            except Exception as exc:
                for i in idxs:
                    results[i] = exc
                continue

            new_values: dict[tuple[int, int], int] = {}
            applied: list[int] = []
            for i in idxs:
                player, ep_delta, tp_delta, mp_delta = changes[i]
                found = _locate_player(all_values, player)
                if found is None:
                    results[i] = RuntimeError(
                        f"**{player.username}** is no longer in **{sheet_name}** — points not added."
                    )
                    continue
                row_idx, section = found
                fresh = _build_player(all_values[row_idx], section["headers"], ws.title, row_idx)
                stats = [
                    (fresh.col_ep, fresh.ep, ep_delta),
                    (fresh.col_tp, fresh.tp, tp_delta),
                    (fresh.col_mp, fresh.mp, mp_delta),
                ]
                if all(col < 0 for col, _, _ in stats):
                    logger.warning("No stat columns for %s in [%s] row %d — EP/TP/MP not updated!",
                                   player.username, sheet_name, row_idx + 1)
                    results[i] = _no_stat_columns_error(player)
                    continue
                for col, current, delta in stats:
                    if col >= 0:
                        key = (row_idx, col)
                        new_values[key] = new_values.get(key, current) + delta
                applied.append(i)
                logger.info("Updating %s in [%s] row %d: EP %+d, TP %+d, MP %+d",
                            player.username, sheet_name, row_idx + 1, ep_delta, tp_delta, mp_delta)

            if not new_values:
                continue
            cells = [
                gspread.Cell(row=r + 1, col=c + 1, value=str(v))
                for (r, c), v in new_values.items()
            ]
            try:
                await ws.update_cells(cells, value_input_option="RAW")
            except Exception as exc:
                logger.exception("Failed to write stats to '%s'.", sheet_name)
                for i in applied:
                    results[i] = exc

    return results


async def update_player_stats(
    player: PlayerRow, *, ep_delta: int, tp_delta: int, mp_delta: int
) -> None:
    (error,) = await apply_stat_changes([(player, ep_delta, tp_delta, mp_delta)])
    if error is not None:
        raise error


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

    # Quick check before taking the lock (re-checked against fresh values below)
    eligible, req_msg = check_promotion_requirements(player, new_rank_upper)
    if not eligible and not force:
        raise ValueError(req_msg)

    async with _write_lock:
        ws = await _get_worksheet_safe(player.sheet_name)
        if ws is None:
            raise RuntimeError(f"Cannot find sheet '{player.sheet_name}'")

        all_values = await ws.get_all_values()
        found = _locate_player(all_values, player)
        if found is None:
            raise RuntimeError(f"Player '{player.username}' not found in sheet '{player.sheet_name}'")
        player_row_idx, target_section = found
        headers = target_section["headers"]

        # Use the points in the sheet right now, not from when the player was looked up
        current = _build_player(all_values[player_row_idx], headers, ws.title, player_row_idx)
        eligible, req_msg = check_promotion_requirements(current, new_rank_upper)
        if not eligible and not force:
            raise ValueError(req_msg)

        # Calculate new point values after deduction
        req = RANK_REQUIREMENTS.get(new_rank_upper)
        if req and not force:
            new_ep = current.ep - req["ep"]
            new_tp = current.tp - req["tp"]
            new_mp = current.mp - req["mp"]
        else:
            new_ep = current.ep
            new_tp = current.tp
            new_mp = current.mp

        # Write rank + deducted points
        row_num = player_row_idx + 1
        updates = []
        rank_col = headers.get("RANK")
        if rank_col is not None:
            updates.append(gspread.Cell(row=row_num, col=rank_col + 1, value=new_rank_upper))
        ep_col = headers.get("EP")
        if ep_col is not None:
            updates.append(gspread.Cell(row=row_num, col=ep_col + 1, value=str(new_ep)))
        tp_col = headers.get("TP")
        if tp_col is not None:
            updates.append(gspread.Cell(row=row_num, col=tp_col + 1, value=str(new_tp)))
        mp_col = headers.get("MP")
        if mp_col is not None and req and req["mp"] > 0:
            updates.append(gspread.Cell(row=row_num, col=mp_col + 1, value=str(new_mp)))

        if updates:
            await ws.update_cells(updates, value_input_option="RAW")

        # Re-read and reorder the section
        all_values = await ws.get_all_values()
        await _rewrite_section_sorted(ws, target_section, all_values)

    logger.info("Promoted %s to %s in [%s], points deducted, section reordered",
                player.username, new_rank_upper, player.sheet_name)

    result = req_msg + f"\n\nRank: **{player.rank}** → **{new_rank_upper}**"
    return result


def _row_is_empty(row: list[str], section: dict) -> bool:
    """A row is free if both its USERNAME and LORE NAME cells are blank."""
    for key in ("USERNAME", "LORE NAME"):
        col = section["headers"].get(key)
        if col is not None and col < len(row) and row[col].strip():
            return False
    return True


def _subdivision_sections(sections: list[dict], sheet_name: str, subdivision: str) -> list[dict]:
    """
    Sections that make up *subdivision* on *sheet_name*, roster tables first.
    Uses the USERNAME column from SUBDIVISION_MAP; if the sheet layout has
    shifted and nothing is at that column, falls back to left-to-right order
    of the roster sections (first subdivision = leftmost).
    """
    subs = SUBDIVISION_MAP.get(sheet_name)
    if not subs or subdivision not in subs:
        return []
    target_col = subs[subdivision]
    matches = [sec for sec in sections if sec["username_col"] == target_col]
    if not matches:
        roster_cols = sorted({sec["username_col"] for sec in sections if "EP" in sec["headers"]})
        pos = list(subs).index(subdivision)
        if pos < len(roster_cols):
            logger.warning("No USERNAME column at %d for %s → %s; using roster column %d instead.",
                           target_col, sheet_name, subdivision, roster_cols[pos])
            matches = [sec for sec in sections if sec["username_col"] == roster_cols[pos]]
    # A small table (e.g. command staff) can share the column; prefer the one with stats
    return sorted(matches, key=lambda sec: "EP" not in sec["headers"])


def _first_empty_row(all_values: list[list[str]], sections: list[dict]) -> tuple[int, dict] | None:
    """(1-based row number, section) of the first free row in the given sections."""
    for section in sections:
        for row_idx, row in _section_rows(all_values, section):
            if _row_is_empty(row, section):
                return row_idx + 1, section
    return None


async def _find_empty_subdivision_row(sheet_name: str, subdivision: str):
    """Returns (worksheet, 1-based row number, section) or None."""
    if not SUBDIVISION_MAP.get(sheet_name, {}).get(subdivision):
        return None
    ws = await _get_worksheet_safe(sheet_name)
    if ws is None:
        return None
    all_values = await ws.get_all_values()
    sections = _subdivision_sections(_find_table_sections(all_values), sheet_name, subdivision)
    found = _first_empty_row(all_values, sections)
    if found is None:
        return None
    return ws, found[0], found[1]


async def find_empty_row_in_section(sheet_name: str, subdivision: str) -> tuple[int, dict] | None:
    """Find the first empty row in a subdivision's section."""
    found = await _find_empty_subdivision_row(sheet_name, subdivision)
    if found is None:
        return None
    _ws, row_num, section = found
    return row_num, section["headers"]


async def _clear_player_row(player: PlayerRow) -> bool:
    """
    Clear all fields for a player in their current sheet.
    Caller must hold _write_lock. Returns False if the player wasn't found.
    """
    ws = await _get_worksheet_safe(player.sheet_name)
    if ws is None:
        return False

    all_values = await ws.get_all_values()
    found = _locate_player(all_values, player)
    if found is None:
        logger.warning("Could not find %s in [%s] to clear — nothing removed.",
                       player.username, player.sheet_name)
        return False

    row_idx, section = found
    clear_cells = [
        gspread.Cell(row=row_idx + 1, col=col + 1, value="")
        for col in sorted(set(section["headers"].values()))
    ]
    await ws.update_cells(clear_cells, value_input_option="RAW")
    return True


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
    async with _write_lock:
        return await _transfer_player_locked(player, dest_sheet, subdivision, new_rank)


async def _transfer_player_locked(
    player: PlayerRow,
    dest_sheet: str,
    subdivision: str,
    new_rank: str,
) -> dict:
    # Use the player's points as they are in the sheet right now
    ws_src = await _get_worksheet_safe(player.sheet_name)
    if ws_src is not None:
        src_values = await ws_src.get_all_values()
        found = _locate_player(src_values, player)
        if found is not None:
            row_idx, section = found
            fresh = _build_player(src_values[row_idx], section["headers"], ws_src.title, row_idx)
            fresh.sheet_name = player.sheet_name
            player = fresh

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
    result = await _find_empty_subdivision_row(dest_sheet, subdivision)
    if result is None:
        raise RuntimeError(
            f"No empty row found in {dest_sheet} → {subdivision}. Section might be full."
        )

    ws_dest, dest_row, dest_section = result
    dest_headers = dest_section["headers"]

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
    if not await _clear_player_row(player):
        logger.warning("Transferred %s but could not clear their old row in %s.",
                       player.username, player.sheet_name)

    # Reorder destination section by rank
    all_values = await ws_dest.get_all_values()
    for section in _find_table_sections(all_values):
        if (section["header_row"], section["username_col"]) == (
            dest_section["header_row"], dest_section["username_col"]
        ):
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

async def find_all_player_rows(username: str, cache: SheetCache | None = None) -> list[PlayerRow]:
    """Find a player across ALL sheets — returns every row they appear in."""
    target = username.strip().lower()
    found: list[PlayerRow] = []

    for sheet_name in config.DETACHMENT_SHEETS:
        try:
            ws, all_values = await _read_sheet(sheet_name, cache)
            if ws is None:
                continue
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
            async with _write_lock:
                cleared = await _clear_player_row(player)
            if not cleared:
                continue
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
    async with _write_lock:
        return await _set_exam_checkmark_locked(username)


async def _set_exam_checkmark_locked(username: str) -> bool:
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


async def find_ancillary_player(username: str, cache: SheetCache | None = None) -> PlayerRow | None:
    """Find a player specifically in the Ancillary sheet."""
    target = username.strip().lower()
    for sheet_name in config.DETACHMENT_SHEETS:
        if not _is_ancillary(sheet_name):
            continue

        ws, all_values = await _read_sheet(sheet_name, cache)
        if ws is None:
            continue

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

class PlayerAlreadyExists(Exception):
    """Raised by add_player_to_sheet when the username is already on that tab."""

    def __init__(self, player: PlayerRow):
        self.player = player
        super().__init__(
            f"**{player.username}** already exists in **{player.sheet_name}** (row {player.row_index})."
        )


def _find_exact_username(all_values: list[list[str]], username: str, ws_title: str) -> PlayerRow | None:
    target = username.strip().lower()
    for section in _find_table_sections(all_values):
        ucol = section["username_col"]
        for row_idx, row in _section_rows(all_values, section):
            if ucol < len(row) and row[ucol].strip().lower() == target:
                return _build_player(row, section["headers"], ws_title, row_idx)
    return None


async def add_player_to_sheet(
    username: str,
    lore_name: str,
    sheet_name: str,
    subdivision: str | None = None,
    rank: str = "RCT",
    extra_fields: dict | None = None,
) -> str:
    """
    Add a new player to a sheet. For Rook/Bastion, subdivision is required.
    For other sheets (Ancillary, Medical, etc.), finds the first section with
    a USERNAME column that has stat columns and inserts there.

    Returns a success message string.
    """
    async with _write_lock:
        return await _add_player_to_sheet_locked(
            username, lore_name, sheet_name, subdivision, rank, extra_fields
        )


async def _add_player_to_sheet_locked(
    username: str,
    lore_name: str,
    sheet_name: str,
    subdivision: str | None,
    rank: str,
    extra_fields: dict | None,
) -> str:
    ws = await _get_worksheet_safe(sheet_name)
    if ws is None:
        raise RuntimeError(f"Cannot find sheet '{sheet_name}'")

    all_values = await ws.get_all_values()
    sections = _find_table_sections(all_values)

    if not sections:
        raise RuntimeError(f"No data sections found in '{sheet_name}'")

    # Only block if they're already on THIS tab (a player can be in Bastion
    # and Device Wielders at once). Exact username only, not lore/first name.
    existing = _find_exact_username(all_values, username, ws.title)
    if existing is not None:
        raise PlayerAlreadyExists(existing)

    if subdivision and sheet_name in SUBDIVISION_MAP:
        targets = _subdivision_sections(sections, sheet_name, subdivision)
        if not targets:
            raise RuntimeError(f"Couldn't find the {subdivision} table in {sheet_name}")
        found = _first_empty_row(all_values, targets)
        if found is None:
            raise RuntimeError(f"No empty row in {sheet_name} → {subdivision}")
    else:
        # First section with data columns (EP or DEVICE STATUS) and an empty row.
        # Skip bare command sections (only USERNAME/RANK/LORE NAME).
        targets = [
            sec for sec in sections
            if "EP" in sec["headers"] or "DEVICE STATUS" in sec["headers"] or len(sec["headers"]) > 3
        ]
        found = _first_empty_row(all_values, targets)
        if found is None:
            raise RuntimeError(f"No empty row found in '{sheet_name}'")

    dest_row, dest_section = found
    headers = dest_section["headers"]

    # Write the player — build fields based on what the section has
    fields = [
        ("USERNAME", username),
        ("LORE NAME", lore_name),
    ]

    # Only add RANK if the section has it
    if "RANK" in headers:
        fields.append(("RANK", rank.strip().upper()))

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
        f"**{username}** ({lore_name}) added to **{location}** as **{rank.strip().upper()}** "
        f"(row {dest_row})"
    )
