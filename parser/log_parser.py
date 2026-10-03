"""
Log parser for Discord Data Manager bot.

Supports two formats:

Format A (labelled):
    Username: Smaxhy
    Lore name: Jessica Cruz
    EP: 2
    TP: 1
    Proof: [screenshot]

Format B (unlabelled — first line is username, second is lore name):
    Smaxhy
    Jessica Cruz
    EP: 2
    TP: 1

Values are returned as written. Logs with any stat above 5 (or below -5)
are rejected by the scan, not clamped here.
"""

from __future__ import annotations

import re
from models.player import ParsedLog

MAX_EP_PER_LOG = 5
MAX_TP_PER_LOG = 5
MAX_MP_PER_LOG = 5

# ── Compiled patterns (case-insensitive) ─────────────────────────────
_RE_USERNAME = re.compile(r"username\s*:\s*(.+)", re.IGNORECASE)
_RE_LORE     = re.compile(r"lore\s*name\s*:\s*(.+)", re.IGNORECASE)

# Match both "EP: 2" / "EP 2" AND "2 EP" / "2EP" (same line only)
_RE_EP       = re.compile(r"(?:\bEP[ \t]*:?[ \t]*([+-]?\d+)|([+-]?\d+)[ \t]*EP\b)", re.IGNORECASE)
_RE_TP       = re.compile(r"(?:\bTP[ \t]*:?[ \t]*([+-]?\d+)|([+-]?\d+)[ \t]*TP\b)", re.IGNORECASE)
_RE_MP       = re.compile(r"(?:\bMP[ \t]*:?[ \t]*([+-]?\d+)|([+-]?\d+)[ \t]*MP\b)", re.IGNORECASE)


def _extract_stat(match) -> int:
    """Get the number from whichever group matched (group 1 or group 2)."""
    if match is None:
        return 0
    return int(match.group(1) or match.group(2))

# Lines to skip when guessing username/lore from unlabelled format
# _SKIP_START: skip if line STARTS with these (urls, stat labels, etc)
_SKIP_START = re.compile(
    r"^(ep|tp|mp|proof|rank|screenshot|image|http|www\.|\[|\!)",
    re.IGNORECASE,
)
# _SKIP_EXACT: skip if line IS exactly one of these keywords
_SKIP_EXACT = re.compile(
    r"^(bastion|rook|ancillary|ancilliary"
    r"|charlie|delta|alpha|bravo"
    r"|peacekeeper|medical|research|cannoneer|valkyrie"
    r"|training|event|scouted|exam|promo"
    r"|squadron|detachment|command)$",
    re.IGNORECASE,
)


def _clamp(value: int, limit: int) -> int:
    """Clamp a value between -limit and +limit."""
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value


def parse_log(content: str) -> ParsedLog | None:
    """
    Attempt to parse a Discord message into a structured log.

    Returns a ParsedLog on success or None if the message isn't
    a recognisable player-activity log.

    Minimum requirements for a valid log:
        - Username present (labelled or first line)
        - At least one of EP / TP / MP present
    """
    if not content or len(content) < 5:
        return None

    # At least one stat must be present for this to be a log
    m_ep = _RE_EP.search(content)
    m_tp = _RE_TP.search(content)
    m_mp = _RE_MP.search(content)

    if not (m_ep or m_tp or m_mp):
        return None

    # ── Try Format A: labelled "Username: xxx" ───────────────────
    m_user = _RE_USERNAME.search(content)
    m_lore = _RE_LORE.search(content)

    username = None
    lore_name = None

    if m_user:
        username = m_user.group(1).strip()
        lore_name = m_lore.group(1).strip() if m_lore else None
    else:
        # ── Format B: unlabelled — guess from plain lines ────────
        # Get non-empty lines that aren't stat lines or URLs
        lines = content.strip().split("\n")
        plain_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if _SKIP_START.match(stripped):
                continue
            if _SKIP_EXACT.match(stripped):
                continue
            # Skip lines that are just stats like "EP: 2" or "1 ep 1 tp"
            if re.match(r"^([+-]?\d+\s*[A-Z]{2}|[A-Z]{2}\s*:?\s*[+-]?\d+)$", stripped, re.IGNORECASE):
                continue
            # Skip lines containing any EP/TP/MP pattern (even mixed like "1 ep 1 tp")
            if _RE_EP.search(stripped) or _RE_TP.search(stripped) or _RE_MP.search(stripped):
                continue
            # Skip lines that look like labelled fields
            if re.match(r"^(username|lore\s*name)\s*:", stripped, re.IGNORECASE):
                continue
            plain_lines.append(stripped)

        if plain_lines:
            username = plain_lines[0]
            if len(plain_lines) >= 2:
                lore_name = plain_lines[1]

    if not username:
        return None

    # Clean up username — remove any trailing colons or extra whitespace
    username = username.strip().rstrip(":")

    # Skip if username looks like a sentence (too many words = probably chat)
    if len(username.split()) > 4:
        return None

    ep = _extract_stat(m_ep)
    tp = _extract_stat(m_tp)
    mp = _extract_stat(m_mp)

    return ParsedLog(
        username=username,
        lore_name=lore_name,
        ep=ep,
        tp=tp,
        mp=mp,
    )
