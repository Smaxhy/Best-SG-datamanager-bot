"""/fix must never add a duplicate or a misread line; /sheetcheck reports what the bot sees."""

import pytest

from commands.sheetcheck import format_report
from services import discord_service as scan
from services import fix_service
from services import sheets_service as sheets
from tests.fakes import FakeChannel, FakeMember, FakeWorksheet, ancillary_sheet, grid, install_sheets, message, rook_sheet


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


async def _plan(messages):
    result = await scan.run_scan(FakeChannel(messages), FakeMember())
    return await fix_service.build_plan(result.issues)


@pytest.mark.parametrize("name,ok", [
    ("Smaxhy", True), ("Toasterpuffs2017_V2", True), ("J_rdie", True), ("player63276327", True),
    ("Training Log", False), ("_lead", False), ("trail_", False), ("a__b", False), ("ab", False),
    ("x" * 21, False), ("Smax-hy", False), ("two_under_scores", False),
])
def test_roblox_username_rule(name, ok):
    assert fix_service.looks_like_username(name) is ok


async def test_misread_line_is_never_added(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(), ancillary_sheet())
    plan = await _plan([message(1, "Rook Training\nJessica Cruz\nEP: 2")])
    assert plan.fixes == []
    assert "doesn't look like a Roblox username" in plan.review[0]


async def test_name_in_an_unreadable_table_is_not_duplicated(monkeypatch):
    # A tab the bot doesn't search and can't parse as a player table
    odd = FakeWorksheet("OLD ROSTER", grid(5, 5))
    odd.values[3][2] = "Newbie"
    tabs = install_sheets(monkeypatch, rook_sheet(), ancillary_sheet(), odd)
    monkeypatch.setattr(sheets.config, "DETACHMENT_SHEETS", ["[D] ROOK", "[C] ANCILLARY"])

    plan = await _plan([message(1, "Username: Newbie\nLore name: Nora Vance\nEP: 1")])
    assert plan.fixes == []
    assert "already on the sheet at OLD ROSTER!C4" in plan.review[0]
    assert tabs["[C] ANCILLARY"].update_calls == []


async def test_really_new_player_is_still_offered(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(), ancillary_sheet())
    plan = await _plan([message(1, "Username: Newbie\nLore name: Nora Vance\nEP: 1")])
    assert [f.kind for f in plan.fixes] == ["add"]


async def test_sheetcheck_reports_tables_missing_tabs_and_duplicates(monkeypatch):
    install_sheets(
        monkeypatch,
        rook_sheet(charlie=[["PVT", "Smaxhy", "J C", "1", "0", "0"]]),
        ancillary_sheet(players=[["RCT", "smaxhy", "J C", "0", "0", "0", "", ""]]),
        FakeWorksheet("NOTES", grid(3, 3)),
    )
    monkeypatch.setattr(sheets.config, "DETACHMENT_SHEETS", ["[D] ROOK", "[C] ANCILLARY", "[U] MEDICAL"])
    report = await sheets.describe_sheets()
    text = "\n".join(format_report(report))

    assert "✅ **[D] ROOK** — 2 table(s), 1 players" in text
    assert "table at G2: 1 players · RANK F, USERNAME G, LORE NAME H, EP I, TP J, MP K" in text
    assert "⚪ **NOTES** — not searched" in text
    assert "no such tab: **[U] MEDICAL**" in text
    assert "[D] ROOK!G3 (Smaxhy) · [C] ANCILLARY!C3 (smaxhy)" in text


def test_a1_notation():
    assert (sheets._a1(0, 0), sheets._a1(6, 1), sheets._a1(0, 26), sheets._a1(9, 27)) == ("A1", "B7", "AA1", "AB10")
