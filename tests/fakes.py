"""In-memory stand-ins for gspread worksheets and Discord objects."""

from __future__ import annotations

import copy
from types import SimpleNamespace


class FakeWorksheet:
    """Mimics the async gspread worksheet calls the bot uses, backed by a 2-D list."""

    def __init__(self, title: str, values: list[list[str]]):
        self.title = title
        self.values = values
        self.update_calls: list[list] = []
        self.update_cell_calls: list[tuple] = []

    async def get_all_values(self) -> list[list[str]]:
        return copy.deepcopy(self.values)

    def _set(self, row: int, col: int, value) -> None:
        # gspread rows/cols are 1-based
        while len(self.values) < row:
            self.values.append([])
        r = self.values[row - 1]
        while len(r) < col:
            r.append("")
        # Sheets reads every cell back as text (checkboxes as "TRUE"/"FALSE")
        r[col - 1] = str(value).upper() if isinstance(value, bool) else value

    async def update_cells(self, cells, value_input_option=None) -> None:
        self.update_calls.append(list(cells))
        for c in cells:
            self._set(c.row, c.col, c.value)

    async def update_cell(self, row: int, col: int, value) -> None:
        self.update_cell_calls.append((row, col, value))
        self._set(row, col, value)

    def cell(self, row: int, col: int) -> str:
        """0-based read helper for assertions."""
        r = self.values[row] if row < len(self.values) else []
        return r[col] if col < len(r) else ""


def grid(rows: int, cols: int) -> list[list[str]]:
    return [["" for _ in range(cols)] for _ in range(rows)]


def put_section(values, header_row: int, first_col: int, headers: list[str], players: list[list[str]]):
    """Write a header row starting at *first_col*, then one row per player beneath it."""
    for i, h in enumerate(headers):
        values[header_row][first_col + i] = h
    for r, player in enumerate(players, start=header_row + 1):
        for i, v in enumerate(player):
            values[r][first_col + i] = v
    return values


STAT_HEADERS = ["RANK", "USERNAME", "LORE NAME", "EP", "TP", "MP"]


def rook_sheet(charlie=(), delta=(), rows: int = 12) -> FakeWorksheet:
    """[D] ROOK with Charlie (USERNAME col 6) and Delta (USERNAME col 15) side by side."""
    values = grid(rows, 21)
    values[0][5] = "ROOK DETACHMENT"
    put_section(values, 1, 5, STAT_HEADERS, list(charlie))
    put_section(values, 1, 14, STAT_HEADERS, list(delta))
    return FakeWorksheet("[D] ROOK", values)


def bastion_sheet(alpha=(), bravo=(), rows: int = 12) -> FakeWorksheet:
    values = grid(rows, 21)
    put_section(values, 1, 5, STAT_HEADERS, list(alpha))
    put_section(values, 1, 14, STAT_HEADERS, list(bravo))
    return FakeWorksheet("[D] BASTION", values)


def ancillary_sheet(players=(), rows: int = 12) -> FakeWorksheet:
    values = grid(rows, 10)
    put_section(values, 1, 1, STAT_HEADERS + ["EXAM", "SCOUTED"], list(players))
    return FakeWorksheet("[C] ANCILLARY", values)


def install_sheets(monkeypatch, *worksheets: FakeWorksheet) -> dict[str, FakeWorksheet]:
    """Route sheets_service worksheet lookups to the given fakes."""
    from services import sheets_service

    by_name = {ws.title: ws for ws in worksheets}

    async def fake_get_ws(name: str):
        return by_name.get(name)

    monkeypatch.setattr(sheets_service, "_get_worksheet_safe", fake_get_ws)
    monkeypatch.setattr(sheets_service.config, "DETACHMENT_SHEETS", list(by_name))
    return by_name


# ── Discord ──────────────────────────────────────────────────────────

def message(msg_id: int, content: str = "", *, bot: bool = False, embeds=()):
    return SimpleNamespace(
        id=msg_id,
        content=content,
        author=SimpleNamespace(bot=bot),
        embeds=list(embeds),
    )


def embed(description: str | None = None, fields=()):
    return SimpleNamespace(
        description=description,
        fields=[SimpleNamespace(name=n, value=v) for n, v in fields],
    )


class FakeChannel:
    def __init__(self, messages, name: str = "rook-logs", channel_id: int = 500):
        self.name = name
        self.id = channel_id
        self._messages = messages
        self.history_kwargs = None

    def history(self, **kwargs):
        self.history_kwargs = kwargs
        msgs = self._messages

        async def gen():
            for m in msgs:
                yield m

        return gen()


class FakeMember:
    def __init__(self, user_id: int = 42, name: str = "Max"):
        self.id = user_id
        self._name = name

    def __str__(self) -> str:
        return self._name
