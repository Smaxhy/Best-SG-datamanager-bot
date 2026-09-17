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

EP and TP are capped at 5 per log entry.
MP is capped at 5 per log entry.
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
_RE_EP       = re.compile(r"\bEP\s*:\s*([+-]?\d+)", re.IGNORECASE)
_RE_TP       = re.compile(r"\bTP\s*:\s*([+-]?\d+)", re.IGNORECASE)
_RE_MP       = re.compile(r"\bMP\s*:\s*([+-]?\d+)", re.IGNORECASE)

# Lines to skip when guessing username/lore from unlabelled format
_SKIP_PATTERNS = re.compile(
    r"^(ep|tp|mp|proof|rank|screenshot|image|http|www\.|\[|\!)",
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
            if _SKIP_PATTERNS.match(stripped):
                continue
            # Skip lines that are just stats like "EP: 2"
            if re.match(r"^[A-Z]{2}\s*:\s*[+-]?\d+$", stripped, re.IGNORECASE):
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

    ep = int(m_ep.group(1)) if m_ep else 0
    tp = int(m_tp.group(1)) if m_tp else 0
    mp = int(m_mp.group(1)) if m_mp else 0

    # ── Apply per-log caps ───────────────────────────────────────
    ep = _clamp(ep, MAX_EP_PER_LOG)
    tp = _clamp(tp, MAX_TP_PER_LOG)
    mp = _clamp(mp, MAX_MP_PER_LOG)

    return ParsedLog(
        username=username,
        lore_name=lore_name,
        ep=ep,
        tp=tp,
        mp=mp,
    )
