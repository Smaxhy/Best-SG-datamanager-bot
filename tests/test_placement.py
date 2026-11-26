"""
Ancillary → Rook/Bastion (and /addlog into a squad) put players in the wrong
place. Each test is one way the old code picked the wrong table or row.
"""

import pytest

from services import sheets_service as sheets
from tests.fakes import FakeWorksheet, ancillary_sheet, grid, install_sheets, put_section, rook_sheet

H = ["RANK", "USERNAME", "LORE NAME", "EP", "TP", "MP"]


def _anc(*players):
    return ancillary_sheet(players=[list(p) + ["", ""] for p in players])


async def _move(monkeypatch, rook, name="Newbie", sub="Charlie", rank="RCT"):
    install_sheets(monkeypatch, rook, _anc(["RCT", name, "Nora Vance", "5", "2", "0"]))
    p = await sheets.find_player(name)
    return await sheets.transfer_player(p, "[D] ROOK", sub, rank)


def _where(ws, name):
    return [(r, c) for r, row in enumerate(ws.values) for c, v in enumerate(row) if v == name]


async def test_divider_row_is_not_a_free_slot(monkeypatch):
    rook = rook_sheet(charlie=[
        ["PVT", "A", "A A", "1", "0", "0"],
        ["LANCE CORPORALS", "", "", "", "", ""],   # banner row inside the table
        ["", "", "", "", "", ""],
    ])
    await _move(monkeypatch, rook)
    assert rook.cell(3, 5) == "LANCE CORPORALS"
    assert _where(rook, "Newbie") == [(4, 6)]


async def test_template_defaults_still_count_as_free(monkeypatch):
    rook = rook_sheet(charlie=[
        ["PVT", "A", "A A", "1", "0", "0"],
        ["RCT", "", "", "0", "0", "0"],            # pre-filled empty slot
    ])
    await _move(monkeypatch, rook)
    assert _where(rook, "Newbie") == [(3, 6)]


async def test_small_command_table_in_the_charlie_column_is_skipped(monkeypatch):
    # Layout shifted one column right; a command table (no EP) now sits where Charlie used to
    values = grid(16, 24)
    put_section(values, 1, 5, ["RANK", "USERNAME", "LORE NAME"], [["SGT", "Boss", "B B"]])
    put_section(values, 5, 6, H, [["PVT", "C1", "C C", "0", "0", "0"]])
    put_section(values, 5, 15, H, [["PVT", "D1", "D D", "0", "0", "0"]])
    rook = FakeWorksheet("[D] ROOK", values)
    await _move(monkeypatch, rook)
    assert _where(rook, "Newbie") == [(7, 7)]   # under C1 in the Charlie roster
    assert rook.cell(3, 6) == ""                # command table untouched


async def test_table_labels_beat_column_positions(monkeypatch):
    # Delta is on the LEFT in this sheet, labelled as such
    values = grid(10, 24)
    values[0][5], values[0][14] = "DELTA SQUAD", "CHARLIE SQUAD"
    put_section(values, 1, 5, H, [["PVT", "D1", "D D", "0", "0", "0"]])
    put_section(values, 1, 14, H, [["PVT", "C1", "C C", "0", "0", "0"]])
    rook = FakeWorksheet("[D] ROOK", values)
    await _move(monkeypatch, rook, sub="Charlie")
    assert _where(rook, "Newbie") == [(3, 15)]


async def test_banner_naming_both_squads_is_ignored(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "C1", "C C", "0", "0", "0"]],
                      delta=[["PVT", "D1", "D D", "0", "0", "0"]])
    rook.values[0][5] = "ROOK — CHARLIE & DELTA"
    await _move(monkeypatch, rook, sub="Delta")
    assert _where(rook, "Newbie") == [(3, 15)]


async def test_full_table_does_not_spill_into_the_table_below(monkeypatch):
    values = grid(14, 24)
    put_section(values, 1, 5, H, [["PVT", "C1", "C C", "0", "0", "0"], ["PVT", "C2", "C C", "0", "0", "0"]])
    # Another table directly under Charlie, header shifted one column
    put_section(values, 4, 4, ["NOTES", "RANK", "USERNAME"], [["x", "SGT", "Other"]])
    rook = FakeWorksheet("[D] ROOK", values)
    install_sheets(monkeypatch, rook, _anc(["RCT", "Newbie", "Nora Vance", "5", "2", "0"]))
    p = await sheets.find_player("Newbie")
    with pytest.raises(RuntimeError, match="No empty row"):
        await sheets.transfer_player(p, "[D] ROOK", "Charlie", "RCT")
    assert _where(rook, "Newbie") == []


async def test_lands_right_under_the_last_player_not_far_below(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "C1", "C C", "0", "0", "0"]], rows=14)
    rook.values[10][7] = "note far below"
    await _move(monkeypatch, rook)
    assert _where(rook, "Newbie") == [(3, 6)]


async def test_transfer_reply_says_which_row(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "C1", "C C", "0", "0", "0"]])
    result = await _move(monkeypatch, rook)
    assert "📍 Placed in **[D] ROOK** row **4**" in result["message"]


async def test_stacked_table_rows_are_not_read_as_players(monkeypatch):
    values = grid(12, 24)
    put_section(values, 1, 5, H, [["PVT", "C1", "C C", "0", "0", "0"]])
    put_section(values, 4, 4, ["RANK", "USERNAME", "LORE NAME", "EP"], [["PVT", "Lower", "L L", "0"]])
    ws = FakeWorksheet("[D] ROOK", values)
    install_sheets(monkeypatch, ws)
    # "L L" sits in Charlie's USERNAME column in the lower table — must not count as a player
    found = await sheets.find_player("L L")
    assert found.username == "Lower"          # matched by lore name in the lower table
    assert found.col_ep == 7                  # ...using the lower table's own columns
