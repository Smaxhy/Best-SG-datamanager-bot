"""
Log parser for Discord Data Manager bot.

Supported formats:

Format A — labelled:
    Username: Smaxhy
    Lore name: Jessica Cruz
    EP: 2
    TP: 1
    MP: 0

Format B — unlabelled:
    Smaxhy
    Jessica Cruz
    EP: 2
    TP: 1

The parser keeps:
    username
    lore_name

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
    Rook Charlie
    Ancillary/Rook

will NEVER be treated as a lore name or username.
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

# Primary detachment / unit words. Any of these appearing in a
# candidate username or lore string means the string is metadata,
# not a person's name.
_PRIMARY_DETACHMENT_WORDS = {
    "rook",
    "bastion",
    "ancillary",
    "peacekeeper",
    "peacekeepers",
    "medical",
    "research",
    "cannoneer",
    "cannoneers",
    "valkyrie",
    "command",
    "quartermaster",
}

# Extra metadata words that can appear in a metadata string but are
# not, on their own, sufficient reason to reject a string.
_METADATA_WORDS = _PRIMARY_DETACHMENT_WORDS | {
    "division",
    "detachment",
    "unit",
    "corps",
    "squad",
    "device",
    "wielders",
    "information",
    "j",
    "j.",
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
}


def _is_metadata(value: str | None) -> bool:
    """
    Return True if *value* looks like detachment / unit metadata rather
    than a real username or lore name.

    Examples that return True:
        Rook
        Bastion
        Ancillary
        Ancillary/Rook
        Rook Charlie
        [D] BASTION
        ancillary/rook idk what to type
    """
    if not value:
        return False

    t = value.strip().lower()

    # Remove bracket prefixes like [d], [c], [u], [s].
    t = re.sub(r"\[[a-z]\]\s*", "", t)

    tokens = [p for p in re.split(r"[\s/,&|]+", t) if p]
    if not tokens:
        return False

    # Any primary detachment word ⇒ definitely metadata.
    if any(tok in _PRIMARY_DETACHMENT_WORDS for tok in tokens):
        return True

    # Strings made entirely of metadata words (e.g. "Alpha Bravo").
    if len(tokens) > 1 and all(tok in _METADATA_WORDS for tok in tokens):
        return True

    return False


# Things that should never be interpreted as username / lore.
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

        # Skip stat-only lines: EP: 2 / TP 3 / 2 EP / 3TP
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
    Lore name is kept separate and never contains detachment metadata.
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

    username: str | None = None
    lore_name: str | None = None

    if m_user:
        username = _clean(m_user.group(1))

        if m_lore:
            candidate_lore = _clean(m_lore.group(1))

            # Never allow detachment metadata to become lore.
            if candidate_lore and not _is_metadata(candidate_lore):
                lore_name = candidate_lore
    else:
        # ─────────────────────────────────────────────────────
        # Unlabelled format
        # ─────────────────────────────────────────────────────

        plain_lines = _extract_unlabelled_lines(content)

        if plain_lines:
            username = plain_lines[0]

            # Look at the remaining lines for a lore name.
            for candidate in plain_lines[1:]:
                if _is_metadata(candidate):
                    continue
                if lore_name is None:
                    lore_name = candidate
                    break

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

    # Username itself should never be detachment metadata.
    if _is_metadata(username):
        return None

    # ─────────────────────────────────────────────────────────
    # Stats + caps
    # ─────────────────────────────────────────────────────────

    ep = _clamp(_extract_stat(m_ep), MAX_EP_PER_LOG)
    tp = _clamp(_extract_stat(m_tp), MAX_TP_PER_LOG)
    mp = _clamp(_extract_stat(m_mp), MAX_MP_PER_LOG)

    # ─────────────────────────────────────────────────────────
    # Return
    # ─────────────────────────────────────────────────────────

    return ParsedLog(
        username=username,
        lore_name=lore_name,
        ep=ep,
        tp=tp,
        mp=mp,
    )
