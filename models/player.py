"""Lightweight data objects used between services — not ORM models."""

from dataclasses import dataclass, field


@dataclass
class ParsedLog:
    """Result of parsing a single Discord message."""
    username: str
    lore_name: str | None = None
    ep: int = 0
    tp: int = 0
    mp: int = 0


@dataclass
class PlayerRow:
    """A player as read from Google Sheets."""
    sheet_name: str       # detachment sheet the player lives in
    row_index: int        # 1-based row number in that sheet
    username: str
    lore_name: str
    rank: str
    ep: int
    tp: int
    mp: int
    # column letters/indices resolved at read-time
    col_ep: int = 0
    col_tp: int = 0
    col_mp: int = 0


@dataclass
class ScanResult:
    """Aggregated numbers for the final summary embed."""
    channel_name: str = ""
    messages_checked: int = 0
    valid_logs: int = 0
    new_processed: int = 0
    already_processed: int = 0
    players_updated: int = 0
    players_not_found: int = 0
    invalid_logs: int = 0
    lore_mismatches: int = 0
    errors: int = 0
    ep_added: int = 0
    tp_added: int = 0
    mp_added: int = 0
    flagged_messages: list = field(default_factory=list)
