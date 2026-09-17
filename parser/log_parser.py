"""
Log parser for Discord Data Manager bot.

This module is intentionally isolated so the format can be changed
without touching the rest of the codebase.

Current expected format (case-insensitive, tolerant of spacing):
    Username: Smaxhy
    Lore name: Jessica Cruz
    EP: 2
    TP: 1
    Proof: [screenshot]          ← ignored

Optional fields:
    MP: 3                        ← added when present

The parser returns None for messages that don't look like logs
(normal chat, bot replies, etc.).
"""

from __future__ import annotations

import re
from models.player import ParsedLog


# ── Compiled patterns (case-insensitive) ─────────────────────────────
_RE_USERNAME = re.compile(r"username\s*:\s*(.+)", re.IGNORECASE)
_RE_LORE     = re.compile(r"lore\s*name\s*:\s*(.+)", re.IGNORECASE)
_RE_EP       = re.compile(r"\bEP\s*:\s*([+-]?\d+)", re.IGNORECASE)
_RE_TP       = re.compile(r"\bTP\s*:\s*([+-]?\d+)", re.IGNORECASE)
_RE_MP       = re.compile(r"\bMP\s*:\s*([+-]?\d+)", re.IGNORECASE)


def parse_log(content: str) -> ParsedLog | None:
    """
    Attempt to parse a Discord message into a structured log.

    Returns a ParsedLog on success or None if the message isn't
    a recognisable player-activity log.

    Minimum requirements for a valid log:
        • Username present
        • At least one of EP / TP / MP present
    """
    if not content or len(content) < 5:
        return None

    m_user = _RE_USERNAME.search(content)
    if not m_user:
        return None

    username = m_user.group(1).strip()
    if not username:
        return None

    # At least one stat must be present
    m_ep = _RE_EP.search(content)
    m_tp = _RE_TP.search(content)
    m_mp = _RE_MP.search(content)

    if not (m_ep or m_tp or m_mp):
        return None

    ep = int(m_ep.group(1)) if m_ep else 0
    tp = int(m_tp.group(1)) if m_tp else 0
    mp = int(m_mp.group(1)) if m_mp else 0

    # Lore name is optional but extracted when present
    m_lore = _RE_LORE.search(content)
    lore_name = m_lore.group(1).strip() if m_lore else None

    return ParsedLog(
        username=username,
        lore_name=lore_name,
        ep=ep,
        tp=tp,
        mp=mp,
    )
