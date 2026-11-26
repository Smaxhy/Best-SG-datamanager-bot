"""
Checkboxes and numbers must stay real checkboxes/numbers when the bot writes.
Writing them as text made the EXAM column show "TRUE" with red corners.
"""

import pytest

from services import sheets_service as sheets
from tests.fakes import FakeWorksheet, ancillary_sheet, grid, install_sheets, put_section, rook_sheet

HEADERS = ["RANK", "USERNAME", "LORE NAME", "EP", "TP", "MP", "EXAM", "SCOUTED"]


def _anc_with_ticks(players, ticked):
    """Ancillary-style section; *ticked* = {(row0, col0): bool} stored as real checkbox values."""
    values = grid(10, 10)
    put_section(values, 1, 1, HEADERS, players)
    ws = FakeWorksheet("[C] ANCILLARY", values)
    for (r, c), v in ticked.items():
        ws.values[r][c] = "TRUE" if v else "FALSE"
        ws.typed[(r, c)] = v
    return ws


def _written(ws):
    """(row0, col0) -> value for every cell the bot sent, last write wins."""
    out = {}
    for call in ws.update_calls:
        for cell in call:
            out[(cell.row - 1, cell.col - 1)] = cell.value
    return out


async def test_resort_moves_checkboxes_with_their_player(monkeypatch):
    # RCT listed above PVT → the re-sort swaps them
    ws = _anc_with_ticks(
        [["RCT", "Low", "L L", "0", "0", "0", "TRUE", "FALSE"],
         ["PVT", "High", "H H", "4", "1", "0", "TRUE", "TRUE"]],
        {(2, 7): True, (2, 8): False, (3, 7): True, (3, 8): True},
    )
    install_sheets(monkeypatch, ws)
    all_values = await ws.get_all_values()
    section = sheets._find_table_sections(all_values)[0]
    await sheets._rewrite_section_sorted(ws, section, all_values)

    assert (ws.cell(2, 2), ws.cell(3, 2)) == ("High", "Low")
    assert ws.typed[(2, 7)] is True and ws.typed[(2, 8)] is True    # High's ticks moved with them
    assert ws.typed[(3, 7)] is True and ws.typed[(3, 8)] is False   # Low's ticks moved with them
    assert ws.update_calls == []  # nothing rewritten as text — Sheets moved the rows


async def test_stat_updates_write_numbers(monkeypatch):
    tabs = install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", "Smaxhy", "J C", "3", "1", "0"]]))
    p = await sheets.find_player("Smaxhy")
    await sheets.update_player_stats(p, ep_delta=2, tp_delta=0, mp_delta=1)
    values = list(_written(tabs["[D] ROOK"]).values())
    assert values and all(isinstance(v, int) for v in values)


async def test_new_player_points_are_numbers(monkeypatch):
    tabs = install_sheets(monkeypatch, ancillary_sheet())
    await sheets.add_player_to_sheet("Newbie", "Nora Vance", "[C] ANCILLARY")
    out = _written(tabs["[C] ANCILLARY"])
    assert sorted(v for v in out.values() if isinstance(v, int)) == [0, 0, 0]
    assert not any(v == "0" for v in out.values())


async def test_repair_fixes_only_text_cells(monkeypatch):
    ws = _anc_with_ticks(
        [["PVT", "A", "A A", "5", "0", "0", "TRUE", "FALSE"],
         ["PVT", "B", "B B", "2", "1", "0", "TRUE", "TRUE"]],
        # Row B is fine already: real ticks and real numbers
        {(3, 7): True, (3, 8): True, (3, 4): 2, (3, 5): 1, (3, 6): 0},
    )
    install_sheets(monkeypatch, ws)
    fixed = await sheets.repair_sheet_types()

    out = _written(ws)
    # Row A: text TRUE/FALSE → checkboxes, text numbers → numbers
    assert out[(2, 7)] is True and out[(2, 8)] is False
    assert (out[(2, 4)], out[(2, 5)], out[(2, 6)]) == (5, 0, 0)
    # Row B untouched
    assert not any(r == 3 for r, _ in out)
    assert fixed == {"[C] ANCILLARY": 5}

    # Running it again finds nothing left to do
    assert await sheets.repair_sheet_types() == {"[C] ANCILLARY": 0}


async def test_repair_leaves_names_and_other_columns_alone(monkeypatch):
    ws = _anc_with_ticks([["PVT", "True", "False Name", "1", "0", "0", "", ""]], {})
    install_sheets(monkeypatch, ws)
    await sheets.repair_sheet_types()
    out = _written(ws)
    assert (2, 2) not in out and (2, 3) not in out  # username "True" stays text
