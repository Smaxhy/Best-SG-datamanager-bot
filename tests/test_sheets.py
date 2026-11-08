"""Tests for sheets_service — table detection, player lookup, and sheet writes (against fake worksheets)."""

import asyncio

import pytest

from models.player import PlayerRow
from services import sheets_service as sheets
from tests.fakes import (
    STAT_HEADERS as STAT, FakeWorksheet, ancillary_sheet, bastion_sheet, grid, install_sheets, put_section,
    rook_sheet,
)


def _player(**overrides) -> PlayerRow:
    base = dict(
        sheet_name="[D] ROOK", row_index=3, username="Bob", lore_name="Bob Jones",
        rank="PVT", ep=0, tp=0, mp=0, col_ep=8, col_tp=9, col_mp=10,
    )
    base.update(overrides)
    return PlayerRow(**base)


def _section_rows(ws: FakeWorksheet, username_col: int, start: int = 2, count: int = 4):
    """(rank, username, ep, tp) tuples for the rows under a section header."""
    return [
        (ws.cell(r, username_col - 1), ws.cell(r, username_col), ws.cell(r, username_col + 2), ws.cell(r, username_col + 3))
        for r in range(start, start + count)
    ]


# ── _find_table_sections ─────────────────────────────────────────────

def test_find_sections_side_by_side():
    ws = rook_sheet()
    sections = sheets._find_table_sections(ws.values)
    assert [(s["header_row"], s["username_col"]) for s in sections] == [(1, 6), (1, 15)]
    assert sections[0]["headers"] == {"USERNAME": 6, "RANK": 5, "LORE NAME": 7, "EP": 8, "TP": 9, "MP": 10}
    assert sections[1]["headers"] == {"USERNAME": 15, "RANK": 14, "LORE NAME": 16, "EP": 17, "TP": 18, "MP": 19}


def test_find_sections_header_variants_and_extra_columns():
    values = grid(3, 12)
    put_section(values, 0, 0, ["Rank", " username ", "LoreName", "ep", "tp", "mp", "Exam", "Scouted"], [])
    (section,) = sheets._find_table_sections(values)
    assert section["headers"] == {
        "USERNAME": 1, "RANK": 0, "LORE NAME": 2, "EP": 3, "TP": 4, "MP": 5, "EXAM": 6, "SCOUTED": 7,
    }


def test_find_sections_stops_at_next_username_header():
    values = grid(2, 10)
    put_section(values, 0, 0, ["USERNAME", "LORE NAME", "USERNAME", "EP"], [])
    first, second = sheets._find_table_sections(values)
    assert first["headers"] == {"USERNAME": 0, "LORE NAME": 1}
    assert second["headers"] == {"USERNAME": 2, "EP": 3}


def test_find_sections_stacked_vertically():
    values = grid(8, 8)
    put_section(values, 0, 0, ["RANK", "USERNAME", "EP"], [["PVT", "A", "1"]])
    put_section(values, 4, 0, ["RANK", "USERNAME", "EP"], [["RCT", "B", "2"]])
    assert [s["header_row"] for s in sheets._find_table_sections(values)] == [0, 4]


def test_find_sections_none():
    assert sheets._find_table_sections([["foo", "bar"], []]) == []


# ── _build_player ────────────────────────────────────────────────────

def test_build_player_parses_numbers_and_blanks():
    headers = {"USERNAME": 1, "RANK": 0, "LORE NAME": 2, "EP": 3, "TP": 4, "MP": 5}
    row = ["PVT", " Bob ", "Bob Jones", "3.0", "", "abc"]
    p = sheets._build_player(row, headers, "[D] ROOK", 4)
    assert (p.username, p.lore_name, p.rank) == ("Bob", "Bob Jones", "PVT")
    assert (p.ep, p.tp, p.mp) == (3, 0, 0)
    assert p.row_index == 5
    assert (p.col_ep, p.col_tp, p.col_mp) == (3, 4, 5)


def test_build_player_short_row_and_missing_columns():
    p = sheets._build_player(["Bob"], {"USERNAME": 0, "EP": 4}, "S", 0)
    assert p.username == "Bob"
    assert (p.ep, p.rank, p.lore_name) == (0, "", "")
    assert (p.col_ep, p.col_tp, p.col_mp) == (4, -1, -1)


# ── Small helpers ────────────────────────────────────────────────────

def test_rank_sort_key():
    keys = [sheets._rank_sort_key(r) for r in ["lcpl", " PFC ", "PVT", "RCT", "SGT", ""]]
    assert keys == [0, 1, 2, 3, 99, 99]


@pytest.mark.parametrize("name,anc,rb", [
    ("[C] ANCILLARY", True, False),
    ("[D] ROOK", False, True),
    ("[d] bastion", False, True),
    ("[U] VALKYRIE", False, False),
])
def test_sheet_kind_helpers(name, anc, rb):
    assert sheets._is_ancillary(name) is anc
    assert sheets._is_rook_or_bastion(name) is rb


# ── check_promotion_requirements ─────────────────────────────────────

@pytest.mark.parametrize("rank,ep,tp,mp", [
    ("PVT", 4, 1, 0),
    ("PFC", 9, 4, 0),
    ("LCPL", 10, 6, 2),
])
def test_promotion_exactly_meets_requirements(rank, ep, tp, mp):
    ok, msg = sheets.check_promotion_requirements(_player(ep=ep, tp=tp, mp=mp), rank)
    assert ok is True
    assert "**0** remaining" in msg


@pytest.mark.parametrize("rank,ep,tp,mp,missing", [
    ("PVT", 3, 1, 0, ["EP: has 3, needs 4"]),
    ("PFC", 9, 3, 0, ["TP: has 3, needs 4"]),
    ("LCPL", 10, 6, 1, ["MP: has 1, needs 2"]),
    ("LCPL", 0, 0, 0, ["EP: has 0, needs 10", "TP: has 0, needs 6", "MP: has 0, needs 2"]),
])
def test_promotion_one_short(rank, ep, tp, mp, missing):
    ok, msg = sheets.check_promotion_requirements(_player(ep=ep, tp=tp, mp=mp), rank)
    assert ok is False
    for m in missing:
        assert m in msg


def test_promotion_remaining_points_and_extra_note():
    ok, msg = sheets.check_promotion_requirements(_player(ep=7, tp=3), " pvt ")
    assert ok is True
    assert "EP: 7 - 4 = **3** remaining" in msg
    assert "TP: 3 - 1 = **2** remaining" in msg
    assert "MP" not in msg
    assert "Scouted + Exam" in msg


def test_promotion_to_rank_without_requirements():
    assert sheets.check_promotion_requirements(_player(), "RCT") == (True, "No requirements defined for this rank.")


# ── find_player ──────────────────────────────────────────────────────

async def test_find_player_by_username_case_insensitive(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", "Smaxhy", "Jessica Cruz", "3", "1", "0"]]))
    p = await sheets.find_player("  SMAXHY ")
    assert (p.sheet_name, p.row_index, p.username, p.ep, p.tp) == ("[D] ROOK", 3, "Smaxhy", 3, 1)
    assert (p.col_ep, p.col_tp, p.col_mp) == (8, 9, 10)


async def test_find_player_searches_every_sheet_and_section(monkeypatch):
    install_sheets(
        monkeypatch,
        rook_sheet(charlie=[["PVT", "A", "Al A", "1", "0", "0"]]),
        bastion_sheet(bravo=[["RCT", "Target", "Tom T", "2", "0", "0"]]),
    )
    p = await sheets.find_player("target")
    assert (p.sheet_name, p.row_index, p.username) == ("[D] BASTION", 3, "Target")
    assert p.col_ep == 17


async def test_find_player_lore_disambiguates_duplicate_usernames(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(
        charlie=[["PVT", "Sam", "Sam Alpha", "1", "0", "0"]],
        delta=[["PVT", "Sam", "Sam Beta", "2", "0", "0"]],
    ))
    assert (await sheets.find_player("Sam")).lore_name == "Sam Alpha"
    assert (await sheets.find_player("Sam", "sam beta")).lore_name == "Sam Beta"


async def test_find_player_match_priority(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[
        ["PVT", "FirstOnly", "Jess Other", "0", "0", "0"],
        ["PVT", "LoreOwner", "jess", "0", "0", "0"],
        ["PVT", "Jess", "Someone Else", "0", "0", "0"],
    ]))
    # 1. exact username beats lore and first-name matches
    assert (await sheets.find_player("Jess")).username == "Jess"


async def test_find_player_lore_as_username_beats_first_name(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[
        ["PVT", "A", "Jessica Cruz", "0", "0", "0"],
        ["PVT", "B", "Jessica", "0", "0", "0"],
    ]))
    assert (await sheets.find_player("jessica")).username == "B"
    assert (await sheets.find_player("Jessica Cruz")).username == "A"


async def test_find_player_prefers_section_with_stat_columns(monkeypatch):
    values = grid(8, 12)
    put_section(values, 0, 0, ["RANK", "USERNAME", "LORE NAME"], [["PVT", "Bob", "Bob J"]])
    put_section(values, 0, 4, ["RANK", "USERNAME", "LORE NAME", "EP", "TP"], [["PVT", "Bob", "Bob J", "4", "2"]])
    install_sheets(monkeypatch, FakeWorksheet("[D] ROOK", values))
    p = await sheets.find_player("Bob")
    assert p.col_ep == 7 and p.ep == 4


async def test_find_player_not_found(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", "A", "Al", "0", "0", "0"]]))
    assert await sheets.find_player("nobody") is None


async def test_find_player_survives_a_broken_sheet(monkeypatch):
    broken = FakeWorksheet("[D] BASTION", [])

    async def boom():
        raise RuntimeError("API down")

    broken.get_all_values = boom
    install_sheets(monkeypatch, broken, rook_sheet(charlie=[["PVT", "Bob", "", "0", "0", "0"]]))
    assert (await sheets.find_player("Bob")).sheet_name == "[D] ROOK"


# ── update_player_stats ──────────────────────────────────────────────

async def test_update_player_stats_writes_new_totals(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Bob", "Bob J", "3", "1", "0"]])
    install_sheets(monkeypatch, ws)
    p = await sheets.find_player("Bob")
    await sheets.update_player_stats(p, ep_delta=2, tp_delta=-1, mp_delta=4)
    assert (ws.cell(2, 8), ws.cell(2, 9), ws.cell(2, 10)) == ("5", "0", "4")


async def test_update_player_stats_without_stat_columns_raises(monkeypatch):
    values = grid(4, 6)
    put_section(values, 0, 0, ["RANK", "USERNAME", "LORE NAME"], [["PVT", "Bob", "Bob J"]])
    install_sheets(monkeypatch, FakeWorksheet("[D] ROOK", values))
    p = await sheets.find_player("Bob")
    assert p.col_ep == -1
    with pytest.raises(RuntimeError, match="no EP/TP/MP columns"):
        await sheets.update_player_stats(p, ep_delta=1, tp_delta=0, mp_delta=0)


async def test_update_player_stats_missing_sheet_raises(monkeypatch):
    install_sheets(monkeypatch)
    with pytest.raises(RuntimeError, match="Cannot find sheet"):
        await sheets.update_player_stats(_player(), ep_delta=1, tp_delta=0, mp_delta=0)


# ── Section sorting & promotion ──────────────────────────────────────

async def test_rewrite_section_sorted_orders_by_rank_and_compacts(monkeypatch):
    ws = rook_sheet(charlie=[
        ["RCT", "R", "", "0", "0", "0"],
        ["", "", "", "", "", ""],
        ["LCPL", "L", "", "1", "0", "0"],
        ["PVT", "P", "", "2", "0", "0"],
    ])
    section = sheets._find_table_sections(ws.values)[0]
    await sheets._rewrite_section_sorted(ws, section, await ws.get_all_values())
    assert [ws.cell(r, 6) for r in range(2, 7)] == ["L", "P", "R", "", ""]
    assert [ws.cell(r, 5) for r in range(2, 5)] == ["LCPL", "PVT", "RCT"]
    # Delta section untouched
    assert ws.cell(1, 15) == "USERNAME"


async def test_promote_rank_deducts_points_and_resorts(monkeypatch):
    ws = rook_sheet(charlie=[
        ["RCT", "A", "Al", "0", "0", "0"],
        ["PVT", "Bob", "Bob J", "10", "5", "1"],
        ["LCPL", "C", "Cy", "0", "0", "0"],
    ])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    msg = await sheets.promote_rank(bob, "pfc")
    assert "Rank: **PVT** → **PFC**" in msg
    assert _section_rows(ws, 6, count=3) == [
        ("LCPL", "C", "0", "0"),
        ("PFC", "Bob", "1", "1"),
        ("RCT", "A", "0", "0"),
    ]
    # MP isn't part of the PFC requirement, so it's left alone
    assert ws.cell(3, 10) == "1"


async def test_promote_rank_not_eligible_raises_and_writes_nothing(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Bob", "Bob J", "1", "0", "0"]])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    with pytest.raises(ValueError, match="does not meet requirements"):
        await sheets.promote_rank(bob, "PFC")
    assert ws.update_calls == []


async def test_promote_rank_force_keeps_points(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Bob", "Bob J", "1", "0", "0"]])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    await sheets.promote_rank(bob, "PFC", force=True)
    assert _section_rows(ws, 6, count=1) == [("PFC", "Bob", "1", "0")]


# ── find_empty_row_in_section ────────────────────────────────────────

async def test_find_empty_row_in_section(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(
        charlie=[["PVT", "A", "", "0", "0", "0"]],
        delta=[["PVT", "B", "", "0", "0", "0"], ["PVT", "C", "", "0", "0", "0"]],
    ))
    row, headers = await sheets.find_empty_row_in_section("[D] ROOK", "Delta")
    assert row == 5 and headers["USERNAME"] == 15
    row, _ = await sheets.find_empty_row_in_section("[D] ROOK", "Charlie")
    assert row == 4


async def test_find_empty_row_unknown_subdivision_or_full(monkeypatch):
    full = [["PVT", f"P{i}", "", "0", "0", "0"] for i in range(3)]
    install_sheets(monkeypatch, rook_sheet(charlie=full, rows=5))
    assert await sheets.find_empty_row_in_section("[D] ROOK", "Nope") is None
    assert await sheets.find_empty_row_in_section("[D] ROOK", "Charlie") is None


# ── transfer_player ──────────────────────────────────────────────────

async def test_transfer_ancillary_to_rook_is_a_promotion(monkeypatch):
    anc = ancillary_sheet([["RCT", "Newbie", "New Guy", "6", "2", "0", "TRUE", "TRUE"]])
    rook = rook_sheet(charlie=[["LCPL", "Vet", "Old Hand", "0", "0", "0"]])
    install_sheets(monkeypatch, anc, rook)
    player = await sheets.find_player("Newbie")
    result = await sheets.transfer_player(player, "[D] ROOK", "Charlie", "PVT")
    assert result["mode"] == "promotion"
    assert (result["new_ep"], result["new_tp"], result["deducted_ep"]) == (2, 1, 4)
    assert _section_rows(rook, 6, count=2) == [("LCPL", "Vet", "0", "0"), ("PVT", "Newbie", "2", "1")]
    # Cleared from Ancillary, including EXAM/SCOUTED
    assert all(anc.cell(2, c) == "" for c in range(1, 9))


async def test_transfer_ancillary_promotion_not_eligible(monkeypatch):
    anc = ancillary_sheet([["RCT", "Newbie", "New Guy", "1", "0", "0", "", ""]])
    rook = rook_sheet()
    install_sheets(monkeypatch, anc, rook)
    player = await sheets.find_player("Newbie")
    with pytest.raises(ValueError):
        await sheets.transfer_player(player, "[D] ROOK", "Charlie", "PVT")
    assert rook.update_calls == [] and anc.update_calls == []


async def test_transfer_rook_to_bastion_keeps_points(monkeypatch):
    rook = rook_sheet(delta=[["PFC", "Mover", "Mo", "7", "3", "1"]])
    bastion = bastion_sheet()
    install_sheets(monkeypatch, rook, bastion)
    player = await sheets.find_player("Mover")
    result = await sheets.transfer_player(player, "[D] BASTION", "Bravo", "PFC")
    assert result["mode"] == "transfer"
    assert (result["new_ep"], result["new_tp"], result["new_mp"]) == (7, 3, 1)
    assert (bastion.cell(2, 15), bastion.cell(2, 17), bastion.cell(2, 19)) == ("Mover", "7", "1")
    assert rook.cell(2, 15) == ""


async def test_transfer_to_other_sheet_resets_points(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "Mover", "Mo", "7", "3", "1"]])
    valk = rook_sheet()
    valk.title = "[U] VALKYRIE"
    install_sheets(monkeypatch, rook, valk)
    player = await sheets.find_player("Mover")
    result = await sheets.transfer_player(player, "[U] VALKYRIE", "Aether", "RCT")
    assert (result["new_ep"], result["new_tp"], result["new_mp"]) == (0, 0, 0)
    assert "reset to 0" in result["message"]


async def test_transfer_full_section_raises_before_writing(monkeypatch):
    rook = rook_sheet(delta=[["PVT", "Mover", "", "0", "0", "0"]])
    full = [["PVT", f"P{i}", "", "0", "0", "0"] for i in range(3)]
    bastion = bastion_sheet(alpha=full, rows=5)
    install_sheets(monkeypatch, rook, bastion)
    player = await sheets.find_player("Mover")
    with pytest.raises(RuntimeError, match="No empty row"):
        await sheets.transfer_player(player, "[D] BASTION", "Alpha")
    assert rook.cell(2, 15) == "Mover"


# ── Deathlog / examcheck / add player ────────────────────────────────

async def test_find_and_remove_all_player_rows(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "Ghost", "Gus Host", "1", "1", "1"], ["PVT", "Keep", "", "0", "0", "0"]])
    anc = ancillary_sheet([["RCT", "Other", "gus", "0", "0", "0", "", ""]])
    install_sheets(monkeypatch, rook, anc)
    found = await sheets.find_all_player_rows("Gus")
    assert sorted(p.username for p in found) == ["Ghost", "Other"]
    assert await sheets.remove_player_rows(found) == 2
    assert rook.cell(2, 6) == "" and rook.cell(2, 8) == ""
    assert rook.cell(3, 6) == "Keep"
    assert anc.cell(2, 2) == ""


async def test_set_exam_checkmark_only_in_ancillary(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "Bob", "", "0", "0", "0"]])
    anc = ancillary_sheet([["RCT", "Bob", "", "0", "0", "0", "FALSE", ""]])
    install_sheets(monkeypatch, rook, anc)
    assert await sheets.set_exam_checkmark("bob") is True
    assert anc.update_cell_calls == [(3, 8, True)]
    assert anc.cell(2, 7) == "TRUE"
    assert rook.update_cell_calls == []
    assert await sheets.set_exam_checkmark("nobody") is False


async def test_find_ancillary_player_by_username_or_lore(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", "Bob", "", "0", "0", "0"]]),
                   ancillary_sheet([["RCT", "Newbie", "New Guy", "0", "0", "0", "", ""]]))
    assert (await sheets.find_ancillary_player("new guy")).username == "Newbie"
    assert await sheets.find_ancillary_player("Bob") is None


async def test_add_player_to_subdivision(monkeypatch):
    rook = rook_sheet(delta=[["PVT", "A", "", "0", "0", "0"]])
    install_sheets(monkeypatch, rook)
    msg = await sheets.add_player_to_sheet("New", "New Guy", "[D] ROOK", "Delta", rank="rct")
    assert "(row 4)" in msg
    assert [rook.cell(3, c) for c in range(14, 20)] == ["RCT", "New", "New Guy", "0", "0", "0"]


async def test_add_player_skips_bare_sections_and_applies_extra_fields(monkeypatch):
    values = grid(6, 12)
    put_section(values, 0, 0, ["RANK", "USERNAME", "LORE NAME"], [["CMD", "Boss", "Big Boss"]])
    put_section(values, 0, 4, ["USERNAME", "LORE NAME", "DEVICE STATUS", "SOURCED BY"], [])
    ws = FakeWorksheet("[U] DEVICES", values)
    install_sheets(monkeypatch, ws)
    await sheets.add_player_to_sheet("New", "New Guy", "[U] DEVICES",
                                     extra_fields={"DEVICE STATUS": "Active", "IGNORED": "x"})
    assert [ws.cell(1, c) for c in range(4, 8)] == ["New", "New Guy", "Active", ""]
    # The bare RANK/USERNAME/LORE NAME section is skipped
    assert ws.cell(1, 1) == "Boss" and ws.cell(2, 1) == ""


# ── Fresh reads, batching and locking ────────────────────────────────

async def test_stat_update_uses_current_sheet_values_not_stale_lookup(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Bob", "Bob J", "3", "1", "0"]])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    ws.values[2][8] = "10"  # someone edits the sheet after the lookup
    await sheets.update_player_stats(bob, ep_delta=2, tp_delta=0, mp_delta=0)
    assert ws.cell(2, 8) == "12"


async def test_stat_update_follows_player_after_resort(monkeypatch):
    ws = rook_sheet(charlie=[
        ["PVT", "Bob", "Bob J", "3", "0", "0"],
        ["PVT", "Al", "Al A", "7", "0", "0"],
    ])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    ws.values[2], ws.values[3] = ws.values[3], ws.values[2]  # section re-sorted
    await sheets.update_player_stats(bob, ep_delta=1, tp_delta=0, mp_delta=0)
    assert (ws.cell(2, 6), ws.cell(2, 8)) == ("Al", "7")
    assert (ws.cell(3, 6), ws.cell(3, 8)) == ("Bob", "4")


async def test_stat_update_for_removed_player_fails_cleanly(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Bob", "Bob J", "3", "0", "0"]])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    ws.values[2][6] = ""
    with pytest.raises(RuntimeError, match="no longer in"):
        await sheets.update_player_stats(bob, ep_delta=1, tp_delta=0, mp_delta=0)
    assert ws.update_calls == []


async def test_apply_stat_changes_batches_per_tab(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "Bob", "", "3", "1", "0"], ["PVT", "Al", "", "0", "0", "0"]])
    bastion = bastion_sheet(alpha=[["PVT", "Cy", "", "5", "0", "0"]])
    install_sheets(monkeypatch, rook, bastion)
    cache = sheets.SheetCache()
    bob = await sheets.find_player("Bob", cache=cache)
    al = await sheets.find_player("Al", cache=cache)
    cy = await sheets.find_player("Cy", cache=cache)
    reads_before = rook.reads + bastion.reads
    results = await sheets.apply_stat_changes([
        (bob, 2, 0, 0), (al, 1, 1, 1), (cy, -1, 0, 0), (bob, 1, 2, 0),
    ])
    assert results == [None, None, None, None]
    # One fresh read and one write per tab
    assert rook.reads + bastion.reads - reads_before == 2
    assert len(rook.update_calls) == 1 and len(bastion.update_calls) == 1
    # Same player twice in one batch: both logs count
    assert (rook.cell(2, 8), rook.cell(2, 9)) == ("6", "3")
    assert (rook.cell(3, 8), rook.cell(3, 9), rook.cell(3, 10)) == ("1", "1", "1")
    assert bastion.cell(2, 8) == "4"


async def test_apply_stat_changes_reports_per_player_errors(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "Bob", "", "3", "1", "0"]])
    install_sheets(monkeypatch, rook)
    bob = await sheets.find_player("Bob")
    ghost = _player(username="Ghost")
    missing_tab = _player(sheet_name="[D] NOPE")
    results = await sheets.apply_stat_changes([(bob, 1, 0, 0), (ghost, 1, 0, 0), (missing_tab, 1, 0, 0)])
    assert results[0] is None
    assert "no longer in" in str(results[1])
    assert "Cannot find sheet" in str(results[2])
    assert rook.cell(2, 8) == "4"


async def test_apply_stat_changes_write_failure_marks_tab_failed(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "Bob", "", "3", "1", "0"]])
    install_sheets(monkeypatch, rook)
    bob = await sheets.find_player("Bob")

    async def boom(cells, value_input_option=None):
        raise RuntimeError("write failed")

    rook.update_cells = boom
    (error,) = await sheets.apply_stat_changes([(bob, 1, 0, 0)])
    assert str(error) == "write failed"


async def test_concurrent_updates_to_same_player_are_not_lost(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Bob", "", "0", "0", "0"]])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    await asyncio.gather(*[
        sheets.update_player_stats(bob, ep_delta=1, tp_delta=0, mp_delta=0) for _ in range(5)
    ])
    assert ws.cell(2, 8) == "5"


async def test_promote_and_scan_update_at_same_time_both_apply(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Bob", "", "10", "5", "0"]])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    await asyncio.gather(
        sheets.promote_rank(bob, "PFC"),
        sheets.update_player_stats(bob, ep_delta=2, tp_delta=0, mp_delta=0),
    )
    # 10 - 9 (promotion) + 2 (log) = 3, whichever ran first
    assert (ws.cell(2, 5), ws.cell(2, 8)) == ("PFC", "3")


async def test_promote_uses_players_current_row(monkeypatch):
    ws = rook_sheet(charlie=[
        ["PVT", "Bob", "", "10", "5", "0"],
        ["RCT", "Al", "", "0", "0", "0"],
    ])
    install_sheets(monkeypatch, ws)
    bob = await sheets.find_player("Bob")
    ws.values[2], ws.values[3] = ws.values[3], ws.values[2]  # rows moved since lookup
    await sheets.promote_rank(bob, "PFC")
    assert _section_rows(ws, 6, count=2) == [("PFC", "Bob", "1", "1"), ("RCT", "Al", "0", "0")]


async def test_clear_row_does_not_wipe_someone_else_after_resort(monkeypatch):
    ws = rook_sheet(charlie=[
        ["PVT", "Ghost", "", "1", "1", "1"],
        ["PVT", "Keep", "", "9", "9", "9"],
    ])
    install_sheets(monkeypatch, ws)
    (ghost,) = await sheets.find_all_player_rows("Ghost")
    ws.values[2], ws.values[3] = ws.values[3], ws.values[2]
    assert await sheets.remove_player_rows([ghost]) == 1
    assert (ws.cell(2, 6), ws.cell(2, 8)) == ("Keep", "9")
    assert ws.cell(3, 6) == ""


async def test_remove_player_rows_skips_players_already_gone(monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Ghost", "", "1", "1", "1"]])
    install_sheets(monkeypatch, ws)
    (ghost,) = await sheets.find_all_player_rows("Ghost")
    ws.values[2][6] = ""
    assert await sheets.remove_player_rows([ghost]) == 0
    assert ws.update_calls == []


# ── SheetCache ───────────────────────────────────────────────────────

async def test_sheet_cache_reads_each_tab_once(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "A", "", "0", "0", "0"], ["PVT", "B", "", "0", "0", "0"]])
    anc = ancillary_sheet([["RCT", "C", "Cy C", "0", "0", "0", "", ""]])
    install_sheets(monkeypatch, rook, anc)
    cache = sheets.SheetCache()
    assert await cache.preload() == {}
    for name in ["A", "B", "nobody", "C"]:
        await sheets.find_player(name, cache=cache)
    await sheets.find_ancillary_player("cy c", cache=cache)
    await sheets.find_all_player_rows("A", cache=cache)
    assert (rook.reads, anc.reads) == (1, 1)


async def test_without_cache_every_lookup_reads(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "A", "", "0", "0", "0"]])
    install_sheets(monkeypatch, rook)
    await sheets.find_player("A")
    await sheets.find_player("A")
    assert rook.reads == 2


async def test_sheet_cache_remembers_failed_tab(monkeypatch):
    broken = FakeWorksheet("[D] BASTION", [])
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        raise RuntimeError("API down")

    broken.get_all_values = boom
    rook = rook_sheet(charlie=[["PVT", "Bob", "", "0", "0", "0"]])
    install_sheets(monkeypatch, broken, rook)
    cache = sheets.SheetCache()
    errors = await cache.preload()
    assert list(errors) == ["[D] BASTION"]
    assert (await sheets.find_player("Bob", cache=cache)).sheet_name == "[D] ROOK"
    assert await sheets.find_player("Nobody", cache=cache) is None
    assert calls == 1


# ── Google API back-off ──────────────────────────────────────────────

async def test_backoff_retries_then_gives_up(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(sheets.asyncio, "sleep", fake_sleep)
    manager = sheets._BackoffClientManager(lambda: None)
    err = RuntimeError("429")

    def method():
        pass

    for _ in range(manager.MAX_RETRIES):
        await manager.handle_gspread_error(err, method, (), {})
    with pytest.raises(RuntimeError, match="429"):
        await manager.handle_gspread_error(err, method, (), {})
    assert len(sleeps) == manager.MAX_RETRIES
    # Exponential, capped at MAX_BACKOFF (+ up to 1s jitter)
    assert sleeps[0] < sleeps[1] < sleeps[2]
    assert all(s <= manager.MAX_BACKOFF + 1 for s in sleeps)
    # Counter resets after giving up
    assert manager._failures == 0


async def test_backoff_counter_resets_after_success(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(sheets.asyncio, "sleep", fake_sleep)
    manager = sheets._BackoffClientManager(lambda: None)

    async def ok_call(self, method, *args, **kwargs):
        return "ok"

    monkeypatch.setattr(sheets.AsyncioGspreadClientManager, "_call", ok_call)
    await manager.handle_requests_error(RuntimeError("timeout"), ok_call, (), {})
    assert manager._failures == 1
    assert await manager._call(ok_call) == "ok"
    assert manager._failures == 0


async def test_transfer_within_same_sheet_clears_the_old_row(monkeypatch):
    rook = rook_sheet(charlie=[["PFC", "Mover", "Mo", "7", "3", "1"], ["PVT", "Stay", "", "2", "0", "0"]])
    install_sheets(monkeypatch, rook)
    player = await sheets.find_player("Mover")
    await sheets.transfer_player(player, "[D] ROOK", "Delta", "PFC")
    # Old row emptied (the source section isn't re-sorted), neighbour untouched
    assert [rook.cell(r, 6) for r in (2, 3)] == ["", "Stay"]
    assert (rook.cell(2, 8), rook.cell(3, 8)) == ("", "2")
    assert (rook.cell(2, 15), rook.cell(2, 17)) == ("Mover", "7")


async def test_backoff_counter_resets_after_non_retryable_error(monkeypatch):
    manager = sheets._BackoffClientManager(lambda: None)

    async def failing_call(self, method, *args, **kwargs):
        self._failures = 3  # pretend earlier retries happened
        raise RuntimeError("403 forbidden")

    monkeypatch.setattr(sheets.AsyncioGspreadClientManager, "_call", failing_call)
    with pytest.raises(RuntimeError):
        await manager._call(failing_call)
    assert manager._failures == 0


# ── /addlog: add_player_to_sheet ─────────────────────────────────────

async def test_add_player_blocks_exact_username_on_same_tab(monkeypatch):
    rook = rook_sheet(delta=[["PVT", "Smaxhy", "Jessica Cruz", "1", "0", "0"]])
    install_sheets(monkeypatch, rook)
    with pytest.raises(sheets.PlayerAlreadyExists, match=r"\*\*Smaxhy\*\* already exists in \*\*\[D\] ROOK\*\* \(row 3\)"):
        await sheets.add_player_to_sheet(" smaxhy ", "Jess", "[D] ROOK", "Charlie")
    assert rook.update_calls == []


async def test_add_player_not_blocked_by_someone_elses_lore_name(monkeypatch):
    # Old check used the fuzzy search, so "Jessica" matched lore "Jessica Cruz"
    anc = ancillary_sheet([["RCT", "Smaxhy", "Jessica Cruz", "0", "0", "0", "", ""]])
    install_sheets(monkeypatch, anc)
    await sheets.add_player_to_sheet("Jessica", "Jessica Hale", "[C] ANCILLARY")
    assert (anc.cell(3, 2), anc.cell(3, 3)) == ("Jessica", "Jessica Hale")


async def test_add_player_allowed_when_on_a_different_tab(monkeypatch):
    rook = rook_sheet(charlie=[["PVT", "Smaxhy", "", "0", "0", "0"]])
    anc = ancillary_sheet()
    install_sheets(monkeypatch, rook, anc)
    await sheets.add_player_to_sheet("Smaxhy", "Jess C", "[C] ANCILLARY")
    assert anc.cell(2, 2) == "Smaxhy"


async def test_add_player_skips_command_table_sharing_the_column(monkeypatch):
    values = grid(14, 21)
    # Small command-staff table at the Charlie column, roster below it
    put_section(values, 0, 5, ["RANK", "USERNAME", "LORE NAME"], [["CPL", "Boss", "Big Boss"]])
    put_section(values, 4, 5, ["RANK", "USERNAME", "LORE NAME", "EP", "TP", "MP"], [["PVT", "A", "Al", "0", "0", "0"]])
    rook = FakeWorksheet("[D] ROOK", values)
    install_sheets(monkeypatch, rook)
    msg = await sheets.add_player_to_sheet("New", "New Guy", "[D] ROOK", "Charlie")
    assert "(row 7)" in msg
    assert (rook.cell(6, 6), rook.cell(6, 8)) == ("New", "0")
    assert rook.cell(2, 6) == ""


async def test_add_player_falls_back_when_columns_shifted(monkeypatch):
    values = grid(8, 24)
    # Same layout as normal but one column further right (USERNAME at 7 and 16)
    put_section(values, 1, 6, STAT, [])
    put_section(values, 1, 15, STAT, [])
    rook = FakeWorksheet("[D] ROOK", values)
    install_sheets(monkeypatch, rook)
    await sheets.add_player_to_sheet("Left", "L", "[D] ROOK", "Charlie")
    await sheets.add_player_to_sheet("Right", "R", "[D] ROOK", "Delta")
    assert (rook.cell(2, 7), rook.cell(2, 16)) == ("Left", "Right")


async def test_add_player_does_not_overwrite_half_filled_row(monkeypatch):
    anc = ancillary_sheet([
        ["RCT", "A", "Al", "0", "0", "0", "", ""],
        ["", "", "Leftover Lore", "3", "", "", "", ""],
    ])
    install_sheets(monkeypatch, anc)
    msg = await sheets.add_player_to_sheet("New", "New Guy", "[C] ANCILLARY")
    assert "(row 5)" in msg
    assert anc.cell(3, 3) == "Leftover Lore"
    assert anc.cell(4, 2) == "New"


async def test_add_player_full_subdivision(monkeypatch):
    full = [["PVT", f"P{i}", "x", "0", "0", "0"] for i in range(3)]
    install_sheets(monkeypatch, rook_sheet(charlie=full, rows=5))
    with pytest.raises(RuntimeError, match="No empty row in"):
        await sheets.add_player_to_sheet("New", "New Guy", "[D] ROOK", "Charlie")



# ── Last-name lookup ─────────────────────────────────────────────────

async def test_find_player_by_unique_last_name(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[
        ["PVT", "Grim_Noob456134", "Shin Fokoumi", "1", "0", "0"],
        ["PVT", "Other", "Ann Vale", "0", "0", "0"],
    ]))
    assert (await sheets.find_player("fokoumi")).username == "Grim_Noob456134"


async def test_find_player_ambiguous_last_name_returns_none(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[
        ["PVT", "A", "Shin Fokoumi", "0", "0", "0"],
        ["PVT", "B", "Ren Fokoumi", "0", "0", "0"],
    ]))
    assert await sheets.find_player("Fokoumi") is None


async def test_first_name_still_beats_last_name(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[
        ["PVT", "A", "Ren Vale", "0", "0", "0"],
        ["PVT", "B", "Vale Hart", "0", "0", "0"],
    ]))
    assert (await sheets.find_player("Vale")).username == "B"
