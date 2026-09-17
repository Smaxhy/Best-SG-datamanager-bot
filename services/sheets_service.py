"""
Google Sheets service — finds players across multi-section sheets.

USERNAME is the authoritative identifier. LORE NAME is used only to
disambiguate duplicate usernames. Matching is normalized to tolerate
case, whitespace and invisible Unicode characters.
"""

from __future__ import annotations

import json
import logging
import re

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


def _norm(value: str | None) -> str:
    """Normalize a Sheets/log value for reliable comparison."""
    if value is None:
        return ""

    value = str(value)
    value = (
        value.replace("\u200b", "")
        .replace("\ufeff", "")
        .replace("\u2060", "")
        .replace("\xa0", " ")
    )
    return re.sub(r"\\s+", " ", value).strip().casefold()


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
    logger.info(
        "Sheet '%s' not found. Available tabs: %s",
        sheet_name,
        available_names,
    )

    target = _norm(sheet_name)
    for ws in all_ws:
        if _norm(ws.title) == target:
            logger.info(
                "Found normalized match: '%s' -> '%s'",
                sheet_name,
                ws.title,
            )
            return ws

    logger.warning("No match for '%s' in any tab.", sheet_name)
    return None


def _find_table_sections(all_values: list[list[str]]) -> list[dict]:
    sections = []

    for row_idx, row in enumerate(all_values):
        username_cols = [
            c for c, cell in enumerate(row)
            if _norm(cell).upper() == "USERNAME"
        ]

        for ucol in username_cols:
            headers = {"USERNAME": ucol}

            for c in range(ucol - 1, max(-1, ucol - 4), -1):
                if _norm(row[c]).upper() == "RANK":
                    headers["RANK"] = c
                    break

            for c in range(ucol + 1, min(len(row), ucol + 9)):
                v = _norm(row[c]).upper()

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
                "Section: row %d, USERNAME col %d, headers=%s",
                row_idx + 1,
                ucol + 1,
                headers,
            )

    return sections


def _build_player(row, headers, ws_title, row_idx) -> PlayerRow:
    def _safe(key, default=""):
        col = headers.get(key)
        if col is None or col >= len(row):
            return default
        return row[col].strip() or default

    def _safe_int(key):
        value = _safe(key, "0")
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return 0

    return PlayerRow(
        sheet_name=ws_title,
        row_index=row_idx + 1,
        username=row[headers["USERNAME"]].strip()
        if headers["USERNAME"] < len(row)
        else "",
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
    username: str,
    lore_name: str | None = None,
) -> PlayerRow | None:
    """
    Safe player lookup.

    Priority:
      1. Exact normalized USERNAME.
      2. If the username occurs more than once, exact LORE NAME can
         disambiguate it.
      3. If no username exists, an exact full LORE NAME can be used as
         a fallback.

    First-name matching has been removed because first names are not
    unique identifiers and can select the wrong player.
    """

    target = _norm(username)
    target_lore = _norm(lore_name)

    if not target:
        logger.warning("Empty username supplied to find_player().")
        return None

    logger.info(
        "Searching for player '%s' (lore: %s)...",
        username,
        lore_name or "any",
    )

    username_matches: list[PlayerRow] = []
    lore_as_username_matches: list[PlayerRow] = []

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

                    if (
                        username_col < len(row)
                        and _norm(row[username_col]).upper() == "USERNAME"
                    ):
                        break

                    if username_col >= len(row):
                        continue

                    cell_user = row[username_col].strip()
                    cell_lore = (
                        row[lore_col].strip()
                        if lore_col is not None and lore_col < len(row)
                        else ""
                    )

                    if not cell_user and not cell_lore:
                        continue

                    cell_user_norm = _norm(cell_user)
                    cell_lore_norm = _norm(cell_lore)

                    # PRIMARY IDENTIFIER: exact username.
                    if cell_user_norm == target:
                        player = _build_player(row, headers, ws.title, row_idx)
                        username_matches.append(player)
                        logger.info(
                            "EXACT USERNAME match: '%s' -> lore='%s', "
                            "sheet='%s', row=%d",
                            cell_user,
                            cell_lore,
                            ws.title,
                            row_idx + 1,
                        )
                        continue

                    # Safe fallback: exact full lore name typed as username.
                    if cell_lore_norm and cell_lore_norm == target:
                        player = _build_player(row, headers, ws.title, row_idx)
                        lore_as_username_matches.append(player)
                        logger.info(
                            "EXACT LORE-AS-USERNAME match: '%s' -> user='%s', "
                            "sheet='%s', row=%d",
                            cell_lore,
                            cell_user,
                            ws.title,
                            row_idx + 1,
                        )

        except Exception:
            logger.exception("Error searching sheet '%s'.", sheet_name)

    # Never let a lore field override a unique exact username match.
    if username_matches:
        if len(username_matches) == 1:
            match = username_matches[0]
            logger.info(
                "Returning exact username match: '%s' in '%s' row %d",
                match.username,
                match.sheet_name,
                match.row_index,
            )
            return match

        # Duplicate usernames require exact lore disambiguation.
        if target_lore:
            exact_lore = [
                p for p in username_matches
                if _norm(p.lore_name) == target_lore
            ]

            if len(exact_lore) == 1:
                match = exact_lore[0]
                logger.info(
                    "Returning duplicate-username match using exact lore: "
                    "'%s' / '%s' in '%s' row %d",
                    match.username,
                    match.lore_name,
                    match.sheet_name,
                    match.row_index,
                )
                return match

        # Refuse to guess between duplicate usernames.
        logger.warning(
            "Multiple exact username matches for '%s'; lore did not "
            "uniquely disambiguate them.",
            username,
        )
        return None

    if len(lore_as_username_matches) == 1:
        match = lore_as_username_matches[0]
        logger.info(
            "Returning exact lore-as-username match: '%s' in '%s' row %d",
            match.username,
            match.sheet_name,
            match.row_index,
        )
        return match

    if len(lore_as_username_matches) > 1:
        logger.warning(
            "Multiple exact lore-name matches for '%s'; refusing to guess.",
            username,
        )
        return None

    logger.warning("Player '%s' not found.", username)
    return None


async def update_player_stats(
    player: PlayerRow,
    *,
    ep_delta: int,
    tp_delta: int,
    mp_delta: int,
) -> None:
    ws = await _get_worksheet_safe(player.sheet_name)

    if ws is None:
        raise RuntimeError(f"Cannot find sheet '{player.sheet_name}' for update")

    updates: list[gspread.Cell] = []

    if player.col_ep >= 0:
        updates.append(
            gspread.Cell(
                row=player.row_index,
                col=player.col_ep + 1,
                value=str(player.ep + ep_delta),
            )
        )

    if player.col_tp >= 0:
        updates.append(
            gspread.Cell(
                row=player.row_index,
                col=player.col_tp + 1,
                value=str(player.tp + tp_delta),
            )
        )

    if player.col_mp >= 0:
        updates.append(
            gspread.Cell(
                row=player.row_index,
                col=player.col_mp + 1,
                value=str(player.mp + mp_delta),
            )
        )

    if updates:
        await ws.update_cells(updates, value_input_option="RAW")
        logger.info(
            "Updated %s in [%s] row %d: EP %+d, TP %+d, MP %+d",
            player.username,
            player.sheet_name,
            player.row_index,
            ep_delta,
            tp_delta,
            mp_delta,
        )
