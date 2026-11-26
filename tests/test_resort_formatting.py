"""
Re-sorting a section after /promote or a transfer must move each player's whole
row — formatting and dropdowns included — not just the values. Rewriting values
alone left players sitting in rows styled for someone else.
"""

from services import sheets_service as sheets
from tests.fakes import FakeWorksheet, bastion_sheet, grid, install_sheets, put_section, rook_sheet


def _style_rows(ws, rows, cols, tag_by_row):
    for r in rows:
        for c in cols:
            ws.formats[(r, c)] = tag_by_row[r]


async def _resort(ws, which=0):
    all_values = await ws.get_all_values()
    section = sheets._find_table_sections(all_values)[which]
    await sheets._rewrite_section_sorted(ws, section, all_values)


async def test_formatting_moves_with_the_player(monkeypatch):
    ws = rook_sheet(charlie=[
        ["RCT", "Rookie", "R R", "0", "0", "0"],
        ["PVT", "Vet", "V V", "5", "2", "0"],
    ])
    install_sheets(monkeypatch, ws)
    # Rookie's row is plain, Vet's row has the purple chip style
    _style_rows(ws, [2, 3], range(5, 11), {2: "plain", 3: "chip"})

    await _resort(ws)

    assert (ws.cell(2, 6), ws.cell(3, 6)) == ("Vet", "Rookie")
    assert ws.formats[(2, 5)] == "chip" and ws.formats[(3, 5)] == "plain"
    assert ws.update_calls == []


async def test_side_by_side_section_is_not_touched(monkeypatch):
    ws = rook_sheet(
        charlie=[["RCT", "C1", "C C", "0", "0", "0"], ["PVT", "C2", "C C", "0", "0", "0"]],
        delta=[["RCT", "D1", "D D", "0", "0", "0"], ["PVT", "D2", "D D", "0", "0", "0"]],
    )
    install_sheets(monkeypatch, ws)
    await _resort(ws, which=0)  # Charlie only

    assert (ws.cell(2, 6), ws.cell(3, 6)) == ("C2", "C1")
    assert (ws.cell(2, 15), ws.cell(3, 15)) == ("D1", "D2")
    (call,) = ws.sort_calls
    assert call[2:4] == (5, 10)  # RANK..MP of Charlie only


async def test_extra_labelled_columns_move_too(monkeypatch):
    values = grid(8, 12)
    put_section(values, 1, 1, ["RANK", "USERNAME", "LORE NAME", "EP", "TP", "MP", "NOTES"], [
        ["RCT", "Low", "L L", "0", "0", "0", "low's note"],
        ["PVT", "High", "H H", "0", "0", "0", "high's note"],
    ])
    ws = FakeWorksheet("[C] ANCILLARY", values)
    install_sheets(monkeypatch, ws)
    await _resort(ws)
    assert (ws.cell(2, 2), ws.cell(2, 7)) == ("High", "high's note")


async def test_trailing_empty_rows_and_content_below_are_left_alone(monkeypatch):
    ws = rook_sheet(charlie=[["RCT", "A", "A A", "0", "0", "0"], ["PVT", "B", "B B", "0", "0", "0"]])
    ws.values[10][3] = "footer note"  # outside the Charlie table
    install_sheets(monkeypatch, ws)
    await _resort(ws)
    (call,) = ws.sort_calls
    assert (call[0], call[1]) == (2, 4)   # header+1 .. last player row
    assert ws.cell(10, 3) == "footer note"


async def test_blank_rank_still_uses_sheets_sort(monkeypatch):
    ws = rook_sheet(charlie=[["", "NoRank", "N N", "0", "0", "0"], ["PVT", "B", "B B", "0", "0", "0"]])
    install_sheets(monkeypatch, ws)
    await _resort(ws)
    assert len(ws.sort_calls) == 1
    assert (ws.cell(2, 6), ws.cell(3, 6)) == ("B", "NoRank")


async def test_gaps_between_players_close_up(monkeypatch):
    ws = rook_sheet(charlie=[["RCT", "A", "A A", "0", "0", "0"], ["", "", "", "", "", ""],
                             ["PVT", "B", "B B", "0", "0", "0"]])
    install_sheets(monkeypatch, ws)
    await _resort(ws)
    assert [ws.cell(r, 6) for r in (2, 3, 4)] == ["B", "A", ""]


async def test_unknown_rank_leaves_table_alone(monkeypatch):
    ws = bastion_sheet(alpha=[["CPL", "Odd", "O O", "0", "0", "0"], ["PVT", "Norm", "N N", "0", "0", "0"]])
    install_sheets(monkeypatch, ws)
    await _resort(ws)
    assert ws.sort_calls == [] and ws.update_calls == []
    assert (ws.cell(2, 6), ws.cell(3, 6)) == ("Odd", "Norm")


async def test_sheets_sort_failure_leaves_table_alone(monkeypatch):
    ws = rook_sheet(charlie=[["RCT", "A", "A A", "0", "0", "0"], ["PVT", "B", "B B", "0", "0", "0"]])
    install_sheets(monkeypatch, ws)

    async def boom(*a, **k):
        raise RuntimeError("You can't sort a range with merged cells")

    monkeypatch.setattr(sheets, "_sort_range", boom)
    await _resort(ws)
    assert ws.update_calls == []  # no copying values between rows
    assert (ws.cell(2, 6), ws.cell(3, 6)) == ("A", "B")


async def test_promote_writes_points_as_numbers(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Smaxhy", "J C", "12", "6", "0"]])
    install_sheets(monkeypatch, ws)
    p = await sheets.find_player("Smaxhy")
    await sheets.promote_rank(p, "PFC")
    written = [c.value for call in ws.update_calls for c in call]
    assert "PFC" in written
    assert all(isinstance(v, int) for v in written if v != "PFC")


def test_sort_range_request_shape(monkeypatch):
    """The real API call: right sheet, row/col window and rank column, A→Z."""
    import asyncio

    sent = {}

    class FakeSS:
        async def batch_update(self, body):
            sent.update(body)

    async def fake_ss():
        return FakeSS()

    monkeypatch.setattr(sheets, "_get_spreadsheet", fake_ss)
    ws = type("WS", (), {"id": 77})()
    asyncio.run(sheets._sort_range(ws, 2, 9, 5, 10, 5))
    req = sent["requests"][0]["sortRange"]
    assert req["range"] == {"sheetId": 77, "startRowIndex": 2, "endRowIndex": 9,
                            "startColumnIndex": 5, "endColumnIndex": 11}
    assert req["sortSpecs"] == [{"dimensionIndex": 5, "sortOrder": "ASCENDING"}]
