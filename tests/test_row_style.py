"""
New rows must look like the rest of their table, and half points must survive.
Mirrors the real sheet: rows styled by hand down to the last player, plain
Arial below, and a TP of 18.5.
"""

import asyncio

import pytest

from services import sheets_service as sheets
from tests.fakes import ancillary_sheet, install_sheets, rook_sheet

STYLED, PLAIN = "Alegreya bold centred", None


def _charlie(players, plain_rows=()):
    ws = rook_sheet(charlie=players, rows=14)
    for r in range(2, 2 + len(players)):
        if r not in plain_rows:
            for c in range(5, 11):
                ws.formats[(r, c)] = STYLED
    return ws


async def test_added_player_gets_the_tables_style(monkeypatch):
    ws = _charlie([["PVT", f"P{i}", "L L", "0", "0", "0"] for i in range(4)])
    install_sheets(monkeypatch, ws)
    await sheets.add_player_to_sheet("Redgatez", "Zorin Draven", "[D] ROOK", "Charlie")
    assert ws.cell(6, 6) == "Redgatez"
    assert all(ws.formats.get((6, c)) == STYLED for c in range(5, 11))


async def test_transferred_player_gets_the_tables_style(monkeypatch):
    rook = _charlie([["PVT", f"P{i}", "L L", "0", "0", "0"] for i in range(4)])
    tabs = install_sheets(monkeypatch, rook,
                          ancillary_sheet(players=[["RCT", "Grim_Noob456134", "Kaito Kurosawa", "4", "2", "0", "", ""]]))
    p = await sheets.find_player("Grim_Noob456134")
    await sheets.transfer_player(p, "[D] ROOK", "Charlie", "RCT")
    (row,) = [r for r in range(len(rook.values)) if rook.cell(r, 6) == "Grim_Noob456134"]
    assert rook.formats.get((row, 6)) == STYLED


async def test_style_copied_only_onto_the_new_row(monkeypatch):
    ws = _charlie([["PVT", f"P{i}", "L L", "0", "0", "0"] for i in range(4)])
    install_sheets(monkeypatch, ws)
    await sheets.add_player_to_sheet("New1", "N N", "[D] ROOK", "Charlie")
    (copy,) = ws.format_copies
    assert copy[1] == [6] and copy[2:] == (5, 10)    # one row, Charlie's columns only


async def test_no_clear_majority_means_no_restyling(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "A", "A A", "0", "0", "0"], ["PVT", "B", "B B", "0", "0", "0"]])
    ws.formats[(2, 6)] = "style one"
    ws.formats[(3, 6)] = "style two"
    install_sheets(monkeypatch, ws)
    await sheets.add_player_to_sheet("New1", "N N", "[D] ROOK", "Charlie")
    assert ws.format_copies == []


async def test_repair_restyles_the_odd_rows_only(monkeypatch):
    players = [["PVT", f"P{i}", "L L", "0", "0", "0"] for i in range(6)]
    ws = _charlie(players, plain_rows=(6, 7))           # bottom two plain, like Redgatez/Grim
    install_sheets(monkeypatch, ws)
    changed = await sheets.repair_row_formatting()
    assert changed == {"[D] ROOK": [7, 8]}               # 1-based sheet rows
    assert all(ws.formats.get((r, 6)) == STYLED for r in range(2, 8))
    assert await sheets.repair_row_formatting() == {"[D] ROOK": []}


async def test_formatting_failure_never_breaks_adding(monkeypatch):
    ws = _charlie([["PVT", "P1", "L L", "0", "0", "0"], ["PVT", "P2", "L L", "0", "0", "0"]])
    install_sheets(monkeypatch, ws)

    async def boom(*a, **k):
        raise RuntimeError("API quota")

    monkeypatch.setattr(sheets, "_read_formats", boom)
    msg = await sheets.add_player_to_sheet("New1", "N N", "[D] ROOK", "Charlie")
    assert "added" in msg and ws.cell(4, 6) == "New1"


# ── half points ──────────────────────────────────────────────────────

async def test_half_points_survive_a_scan_update(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "benja0251348", "Mance", "14", "18.5", "0"]])
    install_sheets(monkeypatch, ws)
    p = await sheets.find_player("benja0251348")
    assert p.tp == 18.5
    await sheets.update_player_stats(p, ep_delta=1, tp_delta=1, mp_delta=0)
    assert ws.typed[(2, 9)] == 19.5 and ws.typed[(2, 8)] == 15
    assert isinstance(ws.typed[(2, 8)], int)


async def test_half_points_survive_promotion_and_transfer(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "Half", "H H", "10.5", "4.5", "0"]])
    install_sheets(monkeypatch, rook)
    await sheets.promote_rank(await sheets.find_player("Half"), "PFC")
    assert (rook.typed[(2, 8)], rook.typed[(2, 9)]) == (1.5, 0.5)

    await sheets.transfer_player(await sheets.find_player("Half"), "[D] ROOK", "Delta", "PFC")
    (row,) = [r for r in range(len(rook.values)) if rook.cell(r, 15) == "Half"]
    assert (rook.typed[(row, 17)], rook.typed[(row, 18)]) == (1.5, 0.5)


def test_coerce_keeps_decimals():
    assert sheets._coerce_for_write("TP", "18.5") == 18.5
    assert sheets._coerce_for_write("TP", "18,5") == 18.5
    assert sheets._coerce_for_write("EP", "4.0") == 4
    assert sheets._coerce_for_write("EP", 4.0) == 4 and isinstance(sheets._coerce_for_write("EP", 4.0), int)


# ── the real API calls ───────────────────────────────────────────────

def test_read_formats_parses_the_api_reply(monkeypatch):
    class SS:
        async def fetch_sheet_metadata(self, params):
            assert params["ranges"] == "'[D] ROOK'!G6:G9"
            return {"sheets": [{"data": [{"startRow": 5, "rowData": [
                {"values": [{"userEnteredFormat": {"textFormat": {"fontFamily": "Alegreya", "bold": True},
                                                   "horizontalAlignment": "CENTER"}}]},
                {"values": [{}]},
                {},
            ]}]}]}

    async def ss():
        return SS()

    monkeypatch.setattr(sheets, "_get_spreadsheet", ss)
    ws = type("WS", (), {"title": "[D] ROOK", "id": 3})()
    out = asyncio.run(sheets._read_formats(ws, 6, [5, 6, 7, 8]))
    assert out[5] == ("Alegreya", None, True, "CENTER")
    assert out[6] == out[7] == out[8] == (None, None, False, None)


def test_copy_format_request_shape(monkeypatch):
    sent = {}

    class SS:
        async def batch_update(self, body):
            sent.update(body)

    async def ss():
        return SS()

    monkeypatch.setattr(sheets, "_get_spreadsheet", ss)
    ws = type("WS", (), {"id": 3})()
    asyncio.run(sheets._copy_format(ws, 5, [31, 32], 5, 10))
    (a, b) = sent["requests"]
    assert a["copyPaste"]["pasteType"] == "PASTE_FORMAT"
    assert a["copyPaste"]["source"] == {"sheetId": 3, "startRowIndex": 5, "endRowIndex": 6,
                                        "startColumnIndex": 5, "endColumnIndex": 11}
    assert b["copyPaste"]["destination"]["startRowIndex"] == 32
