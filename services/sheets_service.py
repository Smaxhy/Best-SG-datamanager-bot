"""
Google Sheets service.

Player lookup rules:

1. Exact USERNAME is the primary identifier.
2. If exactly one username exists, use it.
3. If the username exists in multiple places:
   - use the requested detachment
   - otherwise use exact lore name
4. Never use first-name matching.
5. If no username exists, exact full lore-name matching may be used
   as a fallback.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata

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


# ─────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────

def _norm(value: str | None) -> str:
    """
    Strong normalisation for usernames, lore names and sheet names.
    """

    if value is None:
        return ""

    value = str(value)

    value = (
        value
        .replace("\u200b", " ")
        .replace("\ufeff", " ")
        .replace("\u2060", " ")
        .replace("\xa0", " ")
    )

    value = unicodedata.normalize("NFKC", value)

    value = re.sub(r"\s+", " ", value)

    return value.strip().casefold()


def _sheet_detachment(sheet_name: str | None) -> str:
    """
    Convert a sheet name into a simple detachment name.

    Examples:

        [D] ROOK       -> ROOK
         [D] BASTION   -> BASTION
        [C] ANCILLARY  -> ANCILLARY
        [S] PEACEKEEPER -> PEACEKEEPER
        [U] MEDICAL    -> MEDICAL
    """

    value = _norm(sheet_name)

    if not value:
        return ""

    value = re.sub(r"^\[[^\]]+\]\s*", "", value)

    value = value.strip()

    # Sheet aliases.
    aliases = {
        "j. command": "J. COMMAND",
        "j command": "J. COMMAND",
        "device wielder": "DEVICE WIELDERS",
        "device wielders": "DEVICE WIELDERS",
    }

    return aliases.get(value, value).upper()


def _normalise_detachment(value: str | None) -> str:
    """
    Convert parser detachment values into the same form used by sheets.
    """

    value = _norm(value)

    if not value:
        return ""

    # Remove common words.
    value = re.sub(
        r"\b(?:detachment|division|unit|department|corps)\b",
        "",
        value,
    )

    value = re.sub(r"\s+", " ", value).strip()

    aliases = {
        "rook": "ROOK",
        "bastion": "BASTION",
        "ancillary": "ANCILLARY",
        "peacekeeper": "PEACEKEEPER",
        "peacekeepers": "PEACEKEEPER",
        "medical": "MEDICAL",
        "research": "RESEARCH",
        "cannoneer": "CANNONEER",
        "cannoneers": "CANNONEER",
        "valkyrie": "VALKYRIE",
        "command": "COMMAND",
        "j command": "J. COMMAND",
        "j. command": "J. COMMAND",
        "quartermaster": "QUARTERMASTER",
        "device wielder": "DEVICE WIELDERS",
        "device wielders": "DEVICE WIELDERS",
    }

    return aliases.get(value, value.upper())


# ─────────────────────────────────────────────────────────────
# Google credentials
# ─────────────────────────────────────────────────────────────

def _get_credentials() -> Credentials:
    if config.GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)

        return Credentials.from_service_account_info(
            info,
            scopes=SCOPES,
        )

    return Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=SCOPES,
    )


agcm = AsyncioGspreadClientManager(_get_credentials)


async def _get_spreadsheet():
    client = await agcm.authorize()

    return await client.open_by_key(
        config.GOOGLE_SPREADSHEET_ID
    )


# ─────────────────────────────────────────────────────────────
# Worksheet lookup
# ─────────────────────────────────────────────────────────────

async def _get_worksheet_safe(sheet_name: str):
    ss = await _get_spreadsheet()

    try:
        return await ss.worksheet(sheet_name)

    except gspread.exceptions.WorksheetNotFound:
        pass

    # The actual spreadsheet has a leading space on some tabs,
    # e.g. " [D] BASTION".
    all_ws = await ss.worksheets()

    available_names = [
        ws.title
        for ws in all_ws
    ]

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

    logger.warning(
        "No match for sheet '%s'.",
        sheet_name,
    )

    return None


# ─────────────────────────────────────────────────────────────
# Table detection
# ─────────────────────────────────────────────────────────────

def _find_table_sections(
    all_values: list[list[str]],
) -> list[dict]:

    sections: list[dict] = []

    for row_idx, row in enumerate(all_values):

        username_cols = [
            c
            for c, cell in enumerate(row)
            if _norm(cell).upper() == "USERNAME"
        ]

        for ucol in username_cols:

            headers = {
                "USERNAME": ucol,
            }

            # Look backwards for RANK.
            for c in range(
                ucol - 1,
                max(-1, ucol - 4),
                -1,
            ):
                if _norm(row[c]).upper() == "RANK":
                    headers["RANK"] = c
                    break

            # Look forwards for other fields.
            for c in range(
                ucol + 1,
                min(len(row), ucol + 9),
            ):

                value = _norm(row[c]).upper()

                if value == "USERNAME":
                    break

                if value in (
                    "LORE NAME",
                    "LORENAME",
                    "LORE",
                ):
                    headers.setdefault(
                        "LORE NAME",
                        c,
                    )

                elif value == "EP":
                    headers.setdefault(
                        "EP",
                        c,
                    )

                elif value == "TP":
                    headers.setdefault(
                        "TP",
                        c,
                    )

                elif value == "MP":
                    headers.setdefault(
                        "MP",
                        c,
                    )

            sections.append(
                {
                    "header_row": row_idx,
                    "username_col": ucol,
                    "headers": headers,
                }
            )

            logger.info(
                "Section: row %d, USERNAME col %d, headers=%s",
                row_idx + 1,
                ucol + 1,
                headers,
            )

    return sections


# ─────────────────────────────────────────────────────────────
# Player construction
# ─────────────────────────────────────────────────────────────

def _build_player(
    row,
    headers,
    ws_title,
    row_idx,
) -> PlayerRow:

    def _safe(
        key: str,
        default: str = "",
    ):
        col = headers.get(key)

        if col is None:
            return default

        if col >= len(row):
            return default

        value = str(row[col]).strip()

        return value or default

    def _safe_int(key: str) -> int:

        value = _safe(
            key,
            "0",
        )

        try:
            return int(float(value))

        except (
            ValueError,
            TypeError,
        ):
            return 0

    username_col = headers["USERNAME"]

    username = ""

    if username_col < len(row):
        username = str(
            row[username_col]
        ).strip()

    return PlayerRow(
        sheet_name=ws_title,
        row_index=row_idx + 1,
        username=username,
        lore_name=_safe("LORE NAME"),
        rank=_safe("RANK"),
        ep=_safe_int("EP"),
        tp=_safe_int("TP"),
        mp=_safe_int("MP"),
        col_ep=headers.get("EP", -1),
        col_tp=headers.get("TP", -1),
        col_mp=headers.get("MP", -1),
    )


# ─────────────────────────────────────────────────────────────
# Player lookup
# ─────────────────────────────────────────────────────────────

async def find_player(
    username: str,
    lore_name: str | None = None,
    detachment: str | None = None,
) -> PlayerRow | None:

    target_username = _norm(username)

    target_lore = _norm(lore_name)

    target_detachment = _normalise_detachment(
        detachment
    )

    if not target_username:
        logger.warning(
            "Empty username supplied to find_player()."
        )

        return None

    logger.info(
        "Searching for player '%s' "
        "(lore: %s, detachment: %s)...",
        username,
        lore_name or "any",
        detachment or "any",
    )

    username_matches: list[PlayerRow] = []

    lore_as_username_matches: list[PlayerRow] = []

    # ─────────────────────────────────────────────────────────
    # Search every configured player sheet.
    # ─────────────────────────────────────────────────────────

    for configured_sheet_name in config.DETACHMENT_SHEETS:

        try:

            ws = await _get_worksheet_safe(
                configured_sheet_name
            )

            if ws is None:
                continue

            all_values = await ws.get_all_values()

            logger.info(
                "Sheet '%s': %d rows",
                ws.title,
                len(all_values),
            )

            sections = _find_table_sections(
                all_values
            )

            if not sections:
                continue

            for section in sections:

                header_row = section["header_row"]

                username_col = section["username_col"]

                headers = section["headers"]

                lore_col = headers.get(
                    "LORE NAME"
                )

                for row_idx in range(
                    header_row + 1,
                    len(all_values),
                ):

                    row = all_values[row_idx]

                    # New section reached.
                    if (
                        username_col < len(row)
                        and _norm(
                            row[username_col]
                        ).upper() == "USERNAME"
                    ):
                        break

                    if username_col >= len(row):
                        continue

                    cell_user = str(
                        row[username_col]
                    ).strip()

                    cell_lore = ""

                    if (
                        lore_col is not None
                        and lore_col < len(row)
                    ):
                        cell_lore = str(
                            row[lore_col]
                        ).strip()

                    if not cell_user and not cell_lore:
                        continue

                    cell_user_norm = _norm(
                        cell_user
                    )

                    cell_lore_norm = _norm(
                        cell_lore
                    )

                    # ─────────────────────────────────────
                    # PRIMARY IDENTIFIER
                    # ─────────────────────────────────────

                    if cell_user_norm == target_username:

                        player = _build_player(
                            row,
                            headers,
                            ws.title,
                            row_idx,
                        )

                        username_matches.append(
                            player
                        )

                        logger.info(
                            "EXACT USERNAME match: "
                            "'%s' -> lore='%s', "
                            "sheet='%s', row=%d",
                            cell_user,
                            cell_lore,
                            ws.title,
                            row_idx + 1,
                        )

                        continue

                    # ─────────────────────────────────────
                    # Safe fallback:
                    # exact full lore typed as username.
                    #
                    # No first-name matching.
                    # ─────────────────────────────────────

                    if (
                        cell_lore_norm
                        and cell_lore_norm == target_username
                    ):

                        player = _build_player(
                            row,
                            headers,
                            ws.title,
                            row_idx,
                        )

                        lore_as_username_matches.append(
                            player
                        )

                        logger.info(
                            "EXACT LORE-AS-USERNAME match: "
                            "'%s' -> user='%s', "
                            "sheet='%s', row=%d",
                            cell_lore,
                            cell_user,
                            ws.title,
                            row_idx + 1,
                        )

        except Exception:

            logger.exception(
                "Error searching sheet '%s'.",
                configured_sheet_name,
            )

    # ─────────────────────────────────────────────────────────
    # EXACT USERNAME MATCHES
    # ─────────────────────────────────────────────────────────

    if username_matches:

        # Unique username = use it.
        if len(username_matches) == 1:

            match = username_matches[0]

            logger.info(
                "Returning unique exact username match: "
                "'%s' in '%s' row %d",
                match.username,
                match.sheet_name,
                match.row_index,
            )

            return match

        logger.info(
            "Username '%s' has %d exact matches.",
            username,
            len(username_matches),
        )

        # ─────────────────────────────────────────────
        # 1. Detachment disambiguation
        # ─────────────────────────────────────────────

        if target_detachment:

            detachment_matches = [
                player
                for player in username_matches
                if _sheet_detachment(
                    player.sheet_name
                ) == target_detachment
            ]

            if len(detachment_matches) == 1:

                match = detachment_matches[0]

                logger.info(
                    "Resolved duplicate username using "
                    "detachment '%s': "
                    "'%s' in '%s' row %d",
                    target_detachment,
                    match.username,
                    match.sheet_name,
                    match.row_index,
                )

                return match

            if len(detachment_matches) > 1:

                logger.warning(
                    "Username '%s' has multiple matches "
                    "inside detachment '%s'.",
                    username,
                    target_detachment,
                )

                # Continue to lore matching.

        # ─────────────────────────────────────────────
        # 2. Exact lore disambiguation
        # ─────────────────────────────────────────────

        if target_lore:

            lore_matches = [
                player
                for player in username_matches
                if _norm(player.lore_name)
                == target_lore
            ]

            if len(lore_matches) == 1:

                match = lore_matches[0]

                logger.info(
                    "Resolved duplicate username using "
                    "exact lore '%s': "
                    "'%s' in '%s' row %d",
                    lore_name,
                    match.username,
                    match.sheet_name,
                    match.row_index,
                )

                return match

        # ─────────────────────────────────────────────
        # 3. If detachment + lore were supplied,
        #    require them to identify one row.
        # ─────────────────────────────────────────────

        logger.warning(
            "AMBIGUOUS PLAYER: username '%s' "
            "exists in %d sheet rows and could not "
            "be uniquely resolved.",
            username,
            len(username_matches),
        )

        for player in username_matches:

            logger.warning(
                "  Candidate: username='%s', "
                "lore='%s', sheet='%s', row=%d",
                player.username,
                player.lore_name,
                player.sheet_name,
                player.row_index,
            )

        return None

    # ─────────────────────────────────────────────────────────
    # Lore-as-username fallback
    # ─────────────────────────────────────────────────────────

    if len(lore_as_username_matches) == 1:

        match = lore_as_username_matches[0]

        logger.info(
            "Returning exact lore-as-username match: "
            "'%s' in '%s' row %d",
            match.username,
            match.sheet_name,
            match.row_index,
        )

        return match

    if len(lore_as_username_matches) > 1:

        logger.warning(
            "Multiple exact lore-name matches for '%s'; "
            "refusing to guess.",
            username,
        )

        return None

    # ─────────────────────────────────────────────────────────
    # Nothing found
    # ─────────────────────────────────────────────────────────

    logger.warning(
        "PLAYER NOT FOUND: '%s'",
        username,
    )

    return None


# ─────────────────────────────────────────────────────────────
# Update player stats
# ─────────────────────────────────────────────────────────────

async def update_player_stats(
    player: PlayerRow,
    *,
    ep_delta: int,
    tp_delta: int,
    mp_delta: int,
) -> None:

    ws = await _get_worksheet_safe(
        player.sheet_name
    )

    if ws is None:

        raise RuntimeError(
            f"Cannot find sheet "
            f"'{player.sheet_name}' for update"
        )

    updates: list[gspread.Cell] = []

    if player.col_ep >= 0:

        updates.append(
            gspread.Cell(
                row=player.row_index,
                col=player.col_ep + 1,
                value=str(
                    player.ep + ep_delta
                ),
            )
        )

    if player.col_tp >= 0:

        updates.append(
            gspread.Cell(
                row=player.row_index,
                col=player.col_tp + 1,
                value=str(
                    player.tp + tp_delta
                ),
            )
        )

    if player.col_mp >= 0:

        updates.append(
            gspread.Cell(
                row=player.row_index,
                col=player.col_mp + 1,
                value=str(
                    player.mp + mp_delta
                ),
            )
        )

    if updates:

        await ws.update_cells(
            updates,
            value_input_option="RAW",
        )

        logger.info(
            "Updated %s in [%s] row %d: "
            "EP %+d, TP %+d, MP %+d",
            player.username,
            player.sheet_name,
            player.row_index,
            ep_delta,
            tp_delta,
            mp_delta,
        )
