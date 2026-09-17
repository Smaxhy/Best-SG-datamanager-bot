"""
Log parser for Discord Data Manager bot.

Supported formats:

Format A — labelled:
    Username: Smaxhy
    Lore name: Jessica Cruz
    Detachment: Bastion
    EP: 2
    TP: 1
    MP: 0
    Proof: [screenshot]

Also supports:
    Username: Smaxhy
    Lore name: Jessica Cruz
    Rank: Rook
    EP: 2
    TP: 1

Format B — unlabelled:
    Smaxhy
    Jessica Cruz
    Bastion
    EP: 2
    TP: 1

The parser keeps:
    username
    lore_name
    detachment

separate.

Detachment metadata such as:
    Rook
    Bastion
    Ancillary
    Peacekeeper
    Medical
    Research
    Cannoneer
    Valkyrie

will NEVER be treated as a lore name.
"""

from __future__ import annotations

import re

from models.player import ParsedLog


MAX_EP_PER_LOG = 5
MAX_TP_PER_LOG = 5
MAX_MP_PER_LOG = 5


# ─────────────────────────────────────────────────────────────
# Labelled fields
# ─────────────────────────────────────────────────────────────

_RE_USERNAME = re.compile(
    r"^\s*username\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_RE_LORE = re.compile(
    r"^\s*lore\s*name\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_RE_DETACHMENT = re.compile(
    r"^\s*(?:detachment|division|unit|log|department)\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_RE_RANK = re.compile(
    r"^\s*rank\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# ─────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────

_RE_EP = re.compile(
    r"(?:\bEP[ \t]*:?[ \t]*([+-]?\d+)|([+-]?\d+)[ \t]*EP\b)",
    re.IGNORECASE,
)

_RE_TP = re.compile(
    r"(?:\bTP[ \t]*:?[ \t]*([+-]?\d+)|([+-]?\d+)[ \t]*TP\b)",
    re.IGNORECASE,
)

_RE_MP = re.compile(
    r"(?:\bMP[ \t]*:?[ \t]*([+-]?\d+)|([+-]?\d+)[ \t]*MP\b)",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────
# Known metadata
# ─────────────────────────────────────────────────────────────

DETACHMENT_ALIASES = {
    "rook": "ROOK",
    "rook division": "ROOK",
    "rook detachment": "ROOK",
    "rook unit": "ROOK",

    "bastion": "BASTION",
    "bastion division": "BASTION",
    "bastion detachment": "BASTION",
    "bastion unit": "BASTION",

    "ancillary": "ANCILLARY",
    "ancillary corps": "ANCILLARY",
    "ancillary division": "ANCILLARY",
    "ancillary detachment": "ANCILLARY",

    "peacekeeper": "PEACEKEEPER",
    "peacekeepers": "PEACEKEEPER",

    "medical": "MEDICAL",
    "medical unit": "MEDICAL",

    "research": "RESEARCH",
    "research unit": "RESEARCH",

    "cannoneer": "CANNONEER",
    "cannoneers": "CANNONEER",

    "valkyrie": "VALKYRIE",

    "command": "COMMAND",
    "j command": "J. COMMAND",
    "quartermaster": "QUARTERMASTER",
    "device wielder": "DEVICE WIELDERS",
    "device wielders": "DEVICE WIELDERS",
}


# Things that should never be interpreted as username/lore.
_SKIP_LINE = re.compile(
    r"^(?:"
    r"ep|tp|mp|proof|rank|screenshot|image|"
    r"username|lore\s*name|detachment|division|unit|"
    r"department|log|"
    r"http|https|www\.|"
    r"\[|\!"
    r")",
    re.IGNORECASE,
)


def _clean(value: str | None) -> str | None:
    if not value:
        return None

    value = (
        value
        .replace("\u200b", " ")
        .replace("\ufeff", " ")
        .replace("\u2060", " ")
        .replace("\xa0", " ")
    )

    value = re.sub(r"\s+", " ", value).strip()

    if not value:
        return None

    return value


def _normalise(value: str | None) -> str:
    value = _clean(value)
    if not value:
        return ""

    return value.casefold()


def _extract_stat(match) -> int:
    if match is None:
        return 0

    value = match.group(1) or match.group(2)

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clamp(value: int, limit: int) -> int:
    if value > limit:
        return limit

    if value < -limit:
        return -limit

    return value


def _normalise_detachment(value: str | None) -> str | None:
    """
    Convert common detachment names into a consistent value.

    Examples:
        Bastion -> BASTION
        bastion -> BASTION
        Rook Division -> ROOK
        Ancillary/Rook -> None

    Mixed values are deliberately left out because they do not
    identify one specific sheet.
    """

    value = _clean(value)

    if not value:
        return None

    normalised = _normalise(value)

    # Direct match.
    if normalised in DETACHMENT_ALIASES:
        return DETACHMENT_ALIASES[normalised]

    # Do not guess if multiple detachments are mentioned.
    found = []

    for alias, canonical in DETACHMENT_ALIASES.items():
        if re.search(
            rf"(?<![a-z]){re.escape(alias)}(?![a-z])",
            normalised,
        ):
            if canonical not in found:
                found.append(canonical)

    if len(found) == 1:
        return found[0]

    return None


def _looks_like_detachment(value: str | None) -> bool:
    return _normalise_detachment(value) is not None


def _looks_like_metadata(value: str | None) -> bool:
    """
    Detect strings such as:
        Rook
        Bastion
        Ancillary
        Ancillary/Rook
        Rook Charlie
    """

    value = _clean(value)

    if not value:
        return False

    normalised = _normalise(value)

    # Direct detachment.
    if normalised in DETACHMENT_ALIASES:
        return True

    # Check for known metadata words.
    metadata_words = set(DETACHMENT_ALIASES.keys())

    parts = re.split(r"[/|,;:\-]+", normalised)

    for part in parts:
        part = part.strip()

        if part in metadata_words:
            return True

    # Examples:
    # Rook Charlie
    # Bastion Charlie
    # Ancillary Charlie
    for detachment in (
        "rook",
        "bastion",
        "ancillary",
        "peacekeeper",
        "medical",
        "research",
        "cannoneer",
        "valkyrie",
    ):
        if normalised.startswith(detachment + " "):
            return True

    return False


def _extract_unlabelled_lines(content: str) -> list[str]:
    """
    Extract useful plain lines from an unlabelled log.

    Stat lines, URLs, proof lines, and labelled fields are ignored.
    """

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    result: list[str] = []

    for line in lines:
        stripped = _clean(line)

        if not stripped:
            continue

        if _SKIP_LINE.match(stripped):
            continue

        # Skip stat-only lines:
        # EP: 2
        # TP 3
        # 2 EP
        # 3TP
        if re.fullmatch(
            r"(?:[+-]?\d+\s*[A-Z]{2}|[A-Z]{2}\s*:?\s*[+-]?\d+)",
            stripped,
            re.IGNORECASE,
        ):
            continue

        # Skip labelled fields.
        if re.match(
            r"^(?:username|lore\s*name|detachment|division|unit|"
            r"department|log|rank)\s*:",
            stripped,
            re.IGNORECASE,
        ):
            continue

        result.append(stripped)

    return result


def parse_log(content: str) -> ParsedLog | None:
    """
    Parse a Discord activity log.

    Requirements:
        - username
        - at least one of EP / TP / MP

    Username is always the primary identifier.

    Lore name and detachment are kept separate.
    """

    if not content or len(content.strip()) < 5:
        return None

    # ─────────────────────────────────────────────────────────
    # Stats
    # ─────────────────────────────────────────────────────────

    m_ep = _RE_EP.search(content)
    m_tp = _RE_TP.search(content)
    m_mp = _RE_MP.search(content)

    if not (m_ep or m_tp or m_mp):
        return None

    # ─────────────────────────────────────────────────────────
    # Labelled fields
    # ─────────────────────────────────────────────────────────

    m_user = _RE_USERNAME.search(content)
    m_lore = _RE_LORE.search(content)
    m_detachment = _RE_DETACHMENT.search(content)
    m_rank = _RE_RANK.search(content)

    username: str | None = None
    lore_name: str | None = None
    detachment: str | None = None

    if m_user:
        username = _clean(m_user.group(1))

        if m_lore:
            candidate_lore = _clean(m_lore.group(1))

            # Never allow metadata to become lore.
            if not _looks_like_metadata(candidate_lore):
                lore_name = candidate_lore

        # Explicit detachment.
        if m_detachment:
            detachment = _normalise_detachment(m_detachment.group(1))

        # Some logs may use Rank: Bastion / Rank: Rook etc.
        if detachment is None and m_rank:
            detachment = _normalise_detachment(m_rank.group(1))

    else:
        # ─────────────────────────────────────────────────────
        # Unlabelled format
        # ─────────────────────────────────────────────────────

        plain_lines = _extract_unlabelled_lines(content)

        if plain_lines:
            username = plain_lines[0]

            # The second line can be:
            #   - lore name
            #   - detachment
            #   - metadata
            #
            # Determine which it is instead of blindly calling it lore.
            remaining = plain_lines[1:]

            for candidate in remaining:
                if _looks_like_detachment(candidate):
                    if detachment is None:
                        detachment = _normalise_detachment(candidate)
                    continue

                if _looks_like_metadata(candidate):
                    continue

                if lore_name is None:
                    lore_name = candidate

            # If the second line was a mixed metadata string like:
            # Ancillary/Rook
            # don't treat it as lore.
            if len(plain_lines) >= 2:
                second = plain_lines[1]

                if _looks_like_metadata(second):
                    lore_name = None

    # ─────────────────────────────────────────────────────────
    # Username validation
    # ─────────────────────────────────────────────────────────

    username = _clean(username)

    if not username:
        return None

    username = username.rstrip(":")

    # Reject obvious chat sentences.
    if len(username.split()) > 4:
        return None

    # Reject URLs.
    if re.match(r"^(?:https?://|www\.)", username, re.IGNORECASE):
        return None

    # Username itself should never be a known detachment.
    if _looks_like_detachment(username):
        return None

    # ─────────────────────────────────────────────────────────
    # Stats + caps
    # ─────────────────────────────────────────────────────────

    ep = _clamp(
        _extract_stat(m_ep),
        MAX_EP_PER_LOG,
    )

    tp = _clamp(
        _extract_stat(m_tp),
        MAX_TP_PER_LOG,
    )

    mp = _clamp(
        _extract_stat(m_mp),
        MAX_MP_PER_LOG,
    )

    # ─────────────────────────────────────────────────────────
    # Return
    # ─────────────────────────────────────────────────────────

    return ParsedLog(
        username=username,
        lore_name=lore_name,
        detachment=detachment,
        ep=ep,
        tp=tp,
        mp=mp,
    )
