"""
Google Sheets service — finds players across multi-section sheets.
Searches every tab in config.DETACHMENT_SHEETS by username, lore name, or
first name. All name comparisons go through parser.names.name_key so that
markdown, "@", invisible characters and capitalisation don't cause misses.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import random
import re

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import ValueRenderOption
from gspread_asyncio import AsyncioGspreadClientManager

import config
from models.player import PlayerRow
from parser.names import loose_key, name_key

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


def _header_key(cell: str) -> str:
    """'Lore Name:' / ' LORE  NAME ' / 'lore-name' -> 'LORE NAME'."""
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", cell.upper()).split())


def _is_username_header(cell: str) -> bool:
    """Accept 'USERNAME', 'Username:', 'User Name', 'USERNAME ' etc."""
    return _header_key(cell).replace(" ", "") == "USERNAME"


def _find_table_sections(all_values: list[list[str]]) -> list[dict]:
    sections = []
    for row_idx, row in enumerate(all_values):
        username_cols = [
            c for c, cell in enumerate(row)
            if _is_username_header(cell)
        ]
        for ucol in username_cols:
            headers = {"USERNAME": ucol}
            for c in range(ucol - 1, max(-1, ucol - 4), -1):
                if 0 <= c < len(row):
                    if _header_key(row[c]) == "RANK":
                        headers["RANK"] = c
                        break
            for c in range(ucol + 1, min(len(row), ucol + 9)):
                v = _header_key(row[c])
                if v.replace(" ", "") == "USERNAME":
                    break
                if v in ("LORE NAME", "LORENAME", "LORE", "LORE NAMES") and "LORE NAME" not in headers:
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
    target = name_key(username)
    target_loose = loose_key(username)
    target_lore = name_key(lore_name) or None
    logger.info("Searching for player '%s' (lore: %s)...", username, lore_name or "any")
    if not target:
        return None

    exact_user_lore: PlayerRow | None = None   # username + lore both match
    exact_user: PlayerRow | None = None         # username matches
    exact_lore_as_user: PlayerRow | None = None # full lore name matches target
    first_name: PlayerRow | None = None         # first name of lore matches target
    other_name: list[PlayerRow] = []            # last/middle name of lore matches target
    loose_user: list[PlayerRow] = []            # username matches ignoring spaces/_/./-

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
                    if username_col < len(row) and _is_username_header(row[username_col]):
                        break
                    if username_col >= len(row):
                        continue

                    cell_user = row[username_col].strip()
                    cell_lore = ""
                    if lore_col is not None and lore_col < len(row):
                        cell_lore = row[lore_col].strip()

                    if not cell_user and not cell_lore:
                        continue

                    cell_user_low = name_key(cell_user)
                    cell_lore_low = name_key(cell_lore)

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
                        elif target in lore_parts[1:]:
                            other_name.append(_build_player(row, headers, ws.title, row_idx))

                    # 5. Username equal once separators are ignored ("Smax_hy" vs "Smaxhy")
                    if cell_user_low != target and target_loose and loose_key(cell_user) == target_loose:
                        loose_user.append(_build_player(row, headers, ws.title, row_idx))

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

    # 4. Last (or middle) name — only if it points to exactly one player
    if other_name:
        names = {p.username.lower() for p in other_name}
        if len(names) == 1:
            match = max(other_name, key=lambda p: p.col_ep >= 0)
            logger.info("Returning last-name match: '%s' in '%s'", match.username, match.sheet_name)
            return match
        logger.warning("'%s' matches several players' lore names (%s) — not guessing.",
                       username, ", ".join(sorted(names)))

    # 5. Username typed with/without a space, underscore, dot or dash — only if unambiguous
    if loose_user:
        names = {name_key(p.username) for p in loose_user}
        if len(names) == 1:
            match = max(loose_user, key=lambda p: p.col_ep >= 0)
            logger.info("Returning loose username match: '%s' in '%s'", match.username, match.sheet_name)
            return match

    logger.warning("Player '%s' not found.", username)
    return None


async def suggest_usernames(username: str, cache: SheetCache | None = None, limit: int = 2) -> list[str]:
    """
    Closest usernames on the sheet to *username*, for a "did you mean" hint.
    Never used to award points — only shown to the Data Manager.
    """
    target = loose_key(username)
    if not target:
        return []
    names: dict[str, str] = {}
    for sheet_name in config.DETACHMENT_SHEETS:
        try:
            ws, all_values = await _read_sheet(sheet_name, cache)
        except Exception:
            continue
        if ws is None:
            continue
        for section in _find_table_sections(all_values):
            ucol = section["username_col"]
            for _, row in _section_rows(all_values, section):
                if ucol < len(row) and row[ucol].strip():
                    names.setdefault(loose_key(row[ucol]), row[ucol].strip())
    close = difflib.get_close_matches(target, list(names), n=limit, cutoff=0.75)
    return [names[k] for k in close]


def _section_rows(all_values: list[list[str]], section: dict):
    """Yield (row_idx, row) for the data rows under a section header."""
    ucol = section["username_col"]
    for row_idx in range(section["header_row"] + 1, len(all_values)):
        row = all_values[row_idx]
        if ucol < len(row) and _is_username_header(row[ucol]):
            break
        yield row_idx, row


def _locate_player(all_values: list[list[str]], player: PlayerRow) -> tuple[int, dict] | None:
    """
    Find *player*'s row in freshly read sheet values. Rows move when sections
    get re-sorted or players are transferred, so don't trust row_index blindly.
    Returns (0-based row index, section) or None if they're gone.
    """
    target = name_key(player.username)
    candidates = []
    for section in _find_table_sections(all_values):
        ucol = section["username_col"]
        for row_idx, row in _section_rows(all_values, section):
            if ucol < len(row) and name_key(row[ucol]) == target:
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
                gspread.Cell(row=r + 1, col=c + 1, value=int(v))
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


# Columns that hold checkboxes / whole numbers. Writing "TRUE" or "5" as text
# (RAW strings) breaks them: checkboxes turn into red-flagged text and number
# columns stop adding up. Always send real booleans/ints for these.
CHECKBOX_COLUMNS = {"EXAM", "SCOUTED"}
NUMBER_COLUMNS = {"EP", "TP", "MP"}
_INT_RE = re.compile(r"[+-]?\d+(?:\.0+)?")


def _coerce_for_write(key: str, value):
    """Turn text that should be a checkbox or a number back into the real type."""
    if not isinstance(value, str):
        if key in NUMBER_COLUMNS and isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    v = value.strip()
    if key in CHECKBOX_COLUMNS and v.upper() in ("TRUE", "FALSE"):
        return v.upper() == "TRUE"
    if key in NUMBER_COLUMNS and _INT_RE.fullmatch(v):
        return int(float(v))
    return value


async def _read_typed(ws, fallback: list[list]) -> list[list]:
    """
    Read the tab's real values (True/False for checkboxes, numbers as numbers,
    dates as date serials) rather than the text shown on screen, so rewriting
    rows doesn't turn them into text. Falls back to the text values if needed.
    """
    try:
        typed = await ws.get_values(value_render_option=ValueRenderOption.unformatted)
    except Exception:
        logger.warning("Could not read typed values from '%s'; using display text.",
                       getattr(ws, "title", "?"), exc_info=True)
        return fallback
    return typed if typed is not None else fallback


async def _get_section_players(
    ws, section: dict, all_values: list[list[str]], typed_values: list[list] | None = None
) -> list[dict]:
    """
    Read all players from a section as dicts with their row data.
    Values come from *typed_values* when given (real booleans/numbers).
    """
    header_row = section["header_row"]
    username_col = section["username_col"]
    headers = section["headers"]
    players = []

    for row_idx in range(header_row + 1, len(all_values)):
        row = all_values[row_idx]
        if username_col < len(row) and _is_username_header(row[username_col]):
            break
        if username_col >= len(row):
            continue

        cell_user = row[username_col].strip()
        if not cell_user:
            continue

        # Read all fields for this player
        typed_row = typed_values[row_idx] if typed_values is not None and row_idx < len(typed_values) else row
        data = {}
        for key, col in headers.items():
            value = typed_row[col] if col < len(typed_row) else ""
            data[key] = value.strip() if isinstance(value, str) else value

        players.append({
            "row_idx": row_idx,
            "data": data,
        })

    return players


def _section_span(all_values: list[list[str]], section: dict) -> tuple[int, int]:
    """
    (first col, last col) of a section's table, 0-based inclusive. Covers every
    labelled column to the right of the known ones (notes, dates, …) up to a
    blank header or the next table, so whole player rows move together.
    """
    header = all_values[section["header_row"]]
    cols = section["headers"].values()
    start, end = min(cols), max(cols)
    other_starts = [
        min(sec["headers"].values())
        for sec in _find_table_sections([header])
        if min(sec["headers"].values()) > end
    ]
    limit = min(other_starts) - 1 if other_starts else len(header) - 1
    while end + 1 <= limit and str(header[end + 1]).strip():
        end += 1
    return start, end


async def _sort_range(ws, start_row: int, end_row: int, start_col: int, end_col: int, sort_col: int) -> None:
    """
    Sort rows [start_row, end_row) × cols [start_col, end_col] by *sort_col*
    (A→Z, blanks last) using Sheets' own sort — the same as Data → Sort range.
    Cell formatting, dropdowns and checkboxes move with each row.
    """
    ss = await _get_spreadsheet()
    await ss.batch_update({"requests": [{"sortRange": {
        "range": {
            "sheetId": ws.id,
            "startRowIndex": start_row, "endRowIndex": end_row,
            "startColumnIndex": start_col, "endColumnIndex": end_col + 1,
        },
        "sortSpecs": [{"dimensionIndex": sort_col, "sortOrder": "ASCENDING"}],
    }}]})


async def _rewrite_section_sorted(ws, section: dict, all_values: list[list[str]]) -> None:
    """
    Sort a section's players by rank (LCPL first, RCT last).

    Uses Sheets' own sort so each player's whole row — values *and* formatting,
    dropdown chips, checkboxes — moves together. The A→Z order of the rank
    names is exactly LCPL, PFC, PVT, RCT.

    If that isn't possible (other rank text in the table, or Sheets refuses the
    sort, e.g. because of merged cells) the table is left exactly as it is.
    Copying values between rows instead used to leave columns behind and put
    names next to someone else's data.
    """
    header_row = section["header_row"]
    username_col = section["username_col"]
    rank_col = section["headers"].get("RANK")

    player_rows = [
        row_idx for row_idx, row in _section_rows(all_values, section)
        if username_col < len(row) and str(row[username_col]).strip()
    ]
    if not player_rows or rank_col is None:
        return

    ranks = {
        str(all_values[r][rank_col]).strip().upper() if rank_col < len(all_values[r]) else ""
        for r in player_rows
    }
    odd = sorted(ranks - set(RANK_ORDER) - {""})
    if odd:
        logger.info("Not sorting section at row %d: non-standard ranks %s.", header_row + 1, odd)
        return

    start_col, end_col = _section_span(all_values, section)
    try:
        await _sort_range(ws, header_row + 1, player_rows[-1] + 1, start_col, end_col, rank_col)
        logger.info("Sorted section at row %d (%d players) with Sheets sort.",
                    header_row + 1, len(player_rows))
    except Exception:
        logger.exception("Sheets sort failed for section at row %d; left in its current order.",
                         header_row + 1)


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
    Checks requirements and deducts points (unless *force* — a field promotion,
    which skips both). Only the player's own RANK/EP/TP/MP cells are written;
    the table isn't re-sorted, so no one else's row can move.
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
            updates.append(gspread.Cell(row=row_num, col=ep_col + 1, value=int(new_ep)))
        tp_col = headers.get("TP")
        if tp_col is not None:
            updates.append(gspread.Cell(row=row_num, col=tp_col + 1, value=int(new_tp)))
        mp_col = headers.get("MP")
        if mp_col is not None and req and req["mp"] > 0:
            updates.append(gspread.Cell(row=row_num, col=mp_col + 1, value=int(new_mp)))

        if updates:
            await ws.update_cells(updates, value_input_option="RAW")

    logger.info("Promoted %s to %s in [%s] row %d (force=%s)",
                player.username, new_rank_upper, player.sheet_name, row_num, force)

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
    force: bool = False,
) -> dict:
    """
    Move a player from their current sheet to a new detachment/subdivision.

    Two modes:
    - Ancillary → Rook/Bastion: ranked PROMOTION — checks requirements,
      deducts points, keeps remainder. With *force* (field promotion) the
      requirements are skipped and all points are kept.
    - Rook ↔ Bastion: lateral TRANSFER — keeps all points as-is.

    Returns a dict with: message, mode ("promotion"|"transfer"), old_ep/tp/mp,
    new_ep/tp/mp, deducted_ep/tp/mp, old_rank, new_rank.
    """
    async with _write_lock:
        return await _transfer_player_locked(player, dest_sheet, subdivision, new_rank, force)


async def _transfer_player_locked(
    player: PlayerRow,
    dest_sheet: str,
    subdivision: str,
    new_rank: str,
    force: bool = False,
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
        req = None if force else RANK_REQUIREMENTS.get(new_rank_upper)
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
        ("EP", int(dest_ep)),
        ("TP", int(dest_tp)),
        ("MP", int(dest_mp)),
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
    target = name_key(username)
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
                    if username_col < len(row) and _is_username_header(row[username_col]):
                        break
                    if username_col >= len(row):
                        continue

                    cell_user = name_key(row[username_col])
                    cell_lore = ""
                    if lore_col is not None and lore_col < len(row):
                        cell_lore = name_key(row[lore_col])

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
        target = name_key(username)

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
                if username_col < len(row) and _is_username_header(row[username_col]):
                    break
                if username_col >= len(row):
                    continue

                cell_user = name_key(row[username_col])
                if cell_user == target:
                    # Set the EXAM checkbox to TRUE
                    await ws.update_cell(row_idx + 1, exam_col + 1, True)
                    logger.info("Set EXAM checkmark for %s in %s row %d",
                                username, sheet_name, row_idx + 1)
                    return True

    return False


async def find_ancillary_player(username: str, cache: SheetCache | None = None) -> PlayerRow | None:
    """Find a player specifically in the Ancillary sheet."""
    target = name_key(username)
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
                if username_col < len(row) and _is_username_header(row[username_col]):
                    break
                if username_col >= len(row):
                    continue

                cell_user = name_key(row[username_col])
                lore_col = headers.get("LORE NAME")
                cell_lore = ""
                if lore_col is not None and lore_col < len(row):
                    cell_lore = name_key(row[lore_col])

                if cell_user == target or cell_lore == target:
                    return _build_player(row, headers, ws.title, row_idx)

    return None


# ── Repair (used by /repairsheet) ────────────────────────────────────

async def repair_sheet_types(sheet_names: list[str] | None = None) -> dict[str, int]:
    """
    Turn text "TRUE"/"FALSE" in checkbox columns back into real checkboxes, and
    text numbers in EP/TP/MP back into numbers. Only cells that are actually
    text are rewritten. Returns {tab: cells fixed}; a tab that failed maps to -1.
    """
    fixed: dict[str, int] = {}
    async with _write_lock:
        for sheet_name in sheet_names or config.DETACHMENT_SHEETS:
            try:
                ws = await _get_worksheet_safe(sheet_name)
                if ws is None:
                    continue
                all_values = await ws.get_all_values()
                typed = await _read_typed(ws, all_values)
                cells = []
                for section in _find_table_sections(all_values):
                    columns = {
                        key: col for key, col in section["headers"].items()
                        if key in CHECKBOX_COLUMNS or key in NUMBER_COLUMNS
                    }
                    if not columns:
                        continue
                    for row_idx, _row in _section_rows(all_values, section):
                        trow = typed[row_idx] if row_idx < len(typed) else []
                        for key, col in columns.items():
                            value = trow[col] if col < len(trow) else ""
                            new = _coerce_for_write(key, value)
                            if new is not value and type(new) is not type(value):
                                cells.append(gspread.Cell(row=row_idx + 1, col=col + 1, value=new))
                if cells:
                    await ws.update_cells(cells, value_input_option="RAW")
                fixed[ws.title] = len(cells)
                logger.info("Repaired %d cells in '%s'.", len(cells), ws.title)
            except Exception:
                logger.exception("Could not repair '%s'.", sheet_name)
                fixed[sheet_name] = -1
    return fixed


# ── Fix helpers (used by /fix) ───────────────────────────────────────

async def find_players_by_lore(lore_name: str, cache: SheetCache | None = None) -> list[PlayerRow]:
    """Every row on every tab whose full lore name equals *lore_name* (formatting ignored)."""
    target = name_key(lore_name)
    found: list[PlayerRow] = []
    if not target:
        return found
    for sheet_name in config.DETACHMENT_SHEETS:
        try:
            ws, all_values = await _read_sheet(sheet_name, cache)
        except Exception:
            logger.exception("Could not read '%s' while searching lore names.", sheet_name)
            continue
        if ws is None:
            continue
        for section in _find_table_sections(all_values):
            ucol = section["username_col"]
            lcol = section["headers"].get("LORE NAME")
            if lcol is None:
                continue
            for row_idx, row in _section_rows(all_values, section):
                if lcol < len(row) and ucol < len(row) and row[ucol].strip() and name_key(row[lcol]) == target:
                    found.append(_build_player(row, section["headers"], ws.title, row_idx))
    return found


async def update_player_names(
    player: PlayerRow, *, username: str | None = None, lore_name: str | None = None
) -> None:
    """
    Overwrite a player's USERNAME and/or LORE NAME cell. Re-reads the tab first
    and finds the row again, so a re-sort since the lookup can't hit the wrong row.
    """
    async with _write_lock:
        ws = await _get_worksheet_safe(player.sheet_name)
        if ws is None:
            raise RuntimeError(f"Cannot find sheet '{player.sheet_name}'")
        all_values = await ws.get_all_values()
        found = _locate_player(all_values, player)
        if found is None:
            raise RuntimeError(f"**{player.username}** is no longer in **{player.sheet_name}**.")
        row_idx, section = found
        headers = section["headers"]
        cells = []
        if username is not None:
            cells.append(gspread.Cell(row=row_idx + 1, col=headers["USERNAME"] + 1, value=username))
        if lore_name is not None:
            if "LORE NAME" not in headers:
                raise RuntimeError(f"No LORE NAME column for **{player.username}** in **{player.sheet_name}**.")
            cells.append(gspread.Cell(row=row_idx + 1, col=headers["LORE NAME"] + 1, value=lore_name))
        if cells:
            await ws.update_cells(cells, value_input_option="RAW")
            logger.info("Updated names for %s in [%s] row %d: username=%s lore=%s",
                        player.username, player.sheet_name, row_idx + 1, username, lore_name)


async def all_tab_titles() -> list[str]:
    """Every tab in the spreadsheet, searched or not."""
    ss = await _get_spreadsheet()
    return [ws.title for ws in await ss.worksheets()]


def _a1(row_idx: int, col_idx: int) -> str:
    """0-based (row, col) -> 'B7'."""
    col, letters = col_idx + 1, ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row_idx + 1}"


async def find_name_anywhere(name: str, cache: SheetCache | None = None) -> list[str]:
    """
    Every cell on every tab (not just the player tabs, not just recognised
    tables) whose text is *name*, as 'TAB!A1'. A last check before adding
    someone, so a table the bot can't read doesn't lead to a duplicate.
    """
    target = name_key(name)
    if not target:
        return []
    cache = cache or SheetCache()
    hits: list[str] = []
    for title in await all_tab_titles():
        try:
            ws, values = await cache.read(title)
        except Exception:
            logger.exception("Could not read '%s' while checking for %s.", title, name)
            continue
        for r, row in enumerate(values or []):
            for c, cell in enumerate(row):
                if cell and name_key(str(cell)) == target:
                    hits.append(f"{title}!{_a1(r, c)}")
    return hits


async def describe_sheets() -> dict:
    """What the bot sees: tabs, tables, columns, player counts, duplicate usernames."""
    cache = SheetCache()
    titles = await all_tab_titles()
    searched = {t.strip().lower() for t in config.DETACHMENT_SHEETS}
    report = {"tabs": [], "missing": [], "duplicates": {}}
    seen: dict[str, list[str]] = {}

    present = {t.strip().lower() for t in titles}
    report["missing"] = [t for t in config.DETACHMENT_SHEETS if t.strip().lower() not in present]

    for title in titles:
        entry = {"title": title, "searched": title.strip().lower() in searched, "tables": [], "error": None}
        if entry["searched"]:
            try:
                ws, values = await cache.read(title)
                for section in _find_table_sections(values):
                    players = [
                        (r, row) for r, row in _section_rows(values, section)
                        if section["username_col"] < len(row) and str(row[section["username_col"]]).strip()
                    ]
                    entry["tables"].append({
                        "header_cell": _a1(section["header_row"], section["username_col"]),
                        "columns": {k: _a1(section["header_row"], c)[:-len(str(section["header_row"] + 1))]
                                    for k, c in sorted(section["headers"].items(), key=lambda kv: kv[1])},
                        "players": len(players),
                    })
                    for r, row in players:
                        cell = str(row[section["username_col"]]).strip()
                        seen.setdefault(name_key(cell), []).append(
                            f"{title}!{_a1(r, section['username_col'])} ({cell})"
                        )
            except Exception as exc:
                entry["error"] = str(exc)
        report["tabs"].append(entry)

    report["duplicates"] = {k: v for k, v in seen.items() if len(v) > 1}
    return report


def ancillary_sheet_name() -> str:
    """The Ancillary tab new players go on."""
    for name in config.DETACHMENT_SHEETS:
        if _is_ancillary(name):
            return name
    return "[C] ANCILLARY"


# ── Add player to any sheet ─────────────────────────────────────────

class PlayerAlreadyExists(Exception):
    """Raised by add_player_to_sheet when the username is already on that tab."""

    def __init__(self, player: PlayerRow):
        self.player = player
        super().__init__(
            f"**{player.username}** already exists in **{player.sheet_name}** (row {player.row_index})."
        )


def _find_exact_username(all_values: list[list[str]], username: str, ws_title: str) -> PlayerRow | None:
    target = name_key(username)
    for section in _find_table_sections(all_values):
        ucol = section["username_col"]
        for row_idx, row in _section_rows(all_values, section):
            if ucol < len(row) and name_key(row[ucol]) == target:
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
            fields.append((stat, 0))

    # Apply any extra fields passed from the command (e.g. Device Wielder fields)
    if extra_fields:
        for key, value in extra_fields.items():
            if key in headers:
                fields.append((key, value))

    updates = []
    for key, value in fields:
        col = headers.get(key)
        if col is not None:
            updates.append(gspread.Cell(row=dest_row, col=col + 1, value=_coerce_for_write(key, value)))

    if updates:
        await ws.update_cells(updates, value_input_option="RAW")

    location = f"{sheet_name} → {subdivision}" if subdivision else sheet_name
    logger.info("Added player %s (%s) to %s row %d", username, lore_name, location, dest_row)

    return (
        f"**{username}** ({lore_name}) added to **{location}** as **{rank.strip().upper()}** "
        f"(row {dest_row})"
    )
