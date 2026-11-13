"""
Real players were showing as "not found" when the log or the sheet had
formatting the bot didn't expect. Each test here is one of those cases.
"""

from types import SimpleNamespace

import pytest

from parser.log_parser import parse_log
from parser.names import clean_name, loose_key, name_key
from services import discord_service as scan
from services import sheets_service as sheets
from tests.fakes import FakeChannel, FakeMember, ancillary_sheet, grid, install_sheets, message, put_section, rook_sheet
from tests.fakes import FakeWorksheet


# ── name_key ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "Smaxhy", " SMAXHY ", "**Smaxhy**", "@Smaxhy", "`Smaxhy`", "Smaxhy​",
    "​Smaxhy", "Smaxhy ", "𝐒𝐦𝐚𝐱𝐡𝐲", "Ｓｍａｘｈｙ", "__Smaxhy__", "||Smaxhy||", "Smaxhy:",
])
def test_name_key_ignores_formatting(raw):
    assert name_key(raw) == "smaxhy"


def test_name_key_keeps_real_underscores_and_drops_accents():
    assert name_key("Smax_hy") == "smax_hy"
    assert name_key("Smax\\_hy") == "smax_hy"  # Discord-escaped underscore
    assert name_key("Förscher") == "forscher"
    assert loose_key("Smax_hy") == loose_key("smax hy") == loose_key("Smax.hy") == "smaxhy"


def test_clean_name_keeps_case_for_display():
    assert clean_name("**@Smaxhy**") == "Smaxhy"
    assert clean_name("  Jessica   Cruz ") == "Jessica Cruz"


# ── parser ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "**Username:** Smaxhy\n**EP:** 2",
    "__Username__: Smaxhy\n__EP__: 2",
    "Username: **Smaxhy**\nEP: 2",
    "Username: @Smaxhy\nEP: 2",
    "Username - Smaxhy\nEP: 2",
    "User name: Smaxhy\nEP: 2",
    "Roblox Username: Smaxhy\nEP: 2",
    "User: Smaxhy\nEP: 2",
    "IGN: Smaxhy\nEP: 2",
    "• Username: Smaxhy\n• EP: 2",
    "1. Username: Smaxhy\n2. EP: 2",
    "> Username: Smaxhy\n> EP: 2",
    "Username:Smaxhy​\nEP: 2",
])
def test_parser_reads_formatted_logs(text):
    log = parse_log(text)
    assert log is not None, text
    assert log.username == "Smaxhy"
    assert log.ep == 2


@pytest.mark.parametrize("text", [
    "Username: Smaxhy\n**Lore name:** Jessica Cruz\nEP: 1",
    "Username: Smaxhy\nLore: Jessica Cruz\nEP: 1",
    "Username: Smaxhy\nLorename - Jessica Cruz\nEP: 1",
])
def test_parser_reads_lore_label_variants(text):
    assert parse_log(text).lore_name == "Jessica Cruz"


def test_parser_dash_inside_a_word_is_not_a_label():
    log = parse_log("Smaxhy\nJessica Cruz\nlore-friendly rp\nEP: 1")
    assert log.username == "Smaxhy"
    assert log.lore_name == "Jessica Cruz"


def test_bold_stats_are_counted():
    log = parse_log("Username: Smaxhy\n**EP:** 2\n**TP:** 1\n**MP:** 3")
    assert (log.ep, log.tp, log.mp) == (2, 1, 3)


# ── username options / pings ─────────────────────────────────────────

def test_username_options_handle_brackets_and_dashes():
    assert scan._username_options("Smaxhy (Jessica)") == ["Smaxhy (Jessica)", "Smaxhy"]
    assert scan._username_options("Jessica Cruz | Smaxhy") == ["Smaxhy", "Jessica Cruz"]
    assert scan._username_options("Smaxhy - Jessica") == ["Smaxhy - Jessica", "Smaxhy", "Jessica"]
    assert scan._username_options("Smaxhy") == ["Smaxhy"]


def test_pinged_player_becomes_their_nickname():
    msg = message(1, "Username: <@123>\nEP: 2")
    msg.mentions = [SimpleNamespace(id=123, display_name="Jessica | Smaxhy", name="smaxhy_discord")]
    assert scan._extract_text(msg) == "Username: Jessica | Smaxhy\nEP: 2"


# ── sheet lookups ────────────────────────────────────────────────────

@pytest.mark.parametrize("sheet_cell", ["Smaxhy", "Smaxhy​", "@Smaxhy", " smaxhy ", "Smaxhy "])
async def test_find_player_ignores_junk_in_the_sheet_cell(monkeypatch, sheet_cell):
    install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", sheet_cell, "Jessica Cruz", "3", "1", "0"]]))
    p = await sheets.find_player("**smaxhy**")
    assert p is not None and p.row_index == 3


async def test_find_player_ignores_separator_differences_when_unambiguous(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", "Smax_hy", "Jessica Cruz", "3", "1", "0"]]))
    assert (await sheets.find_player("Smaxhy")).username == "Smax_hy"


async def test_find_player_separator_match_refuses_to_guess(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(
        charlie=[["PVT", "Smax_hy", "A A", "0", "0", "0"]],
        delta=[["PVT", "Smax.hy", "B B", "0", "0", "0"]],
    ))
    assert await sheets.find_player("Smaxhy") is None


async def test_exact_username_still_beats_separator_match(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(
        charlie=[["PVT", "Smax_hy", "A A", "0", "0", "0"]],
        delta=[["PVT", "Smaxhy", "B B", "0", "0", "0"]],
    ))
    assert (await sheets.find_player("smaxhy")).username == "Smaxhy"


@pytest.mark.parametrize("header", ["Username", "USERNAME:", "User Name", " username ", "USER-NAME"])
async def test_username_header_variants_are_found(monkeypatch, header):
    values = grid(6, 8)
    put_section(values, 1, 1, ["Rank", header, "Lore Name:", "EP", "TP", "MP"],
                [["PVT", "Smaxhy", "Jessica Cruz", "4", "0", "0"]])
    install_sheets(monkeypatch, FakeWorksheet("[D] ROOK", values))
    p = await sheets.find_player("Smaxhy")
    assert p is not None
    assert (p.lore_name, p.ep, p.col_ep) == ("Jessica Cruz", 4, 4)


async def test_suggestions_point_at_near_misses(monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[
        ["PVT", "Smaxhy", "Jessica Cruz", "0", "0", "0"],
        ["PVT", "Totally", "Other Person", "0", "0", "0"],
    ]))
    assert await sheets.suggest_usernames("Smaxy") == ["Smaxhy"]
    assert await sheets.suggest_usernames("zzzz") == []


async def test_ancillary_and_deathlog_lookups_use_same_matching(monkeypatch):
    install_sheets(monkeypatch, ancillary_sheet(players=[["RCT", "Smaxhy​", "Jessica Cruz", "0", "0", "0", "", ""]]))
    assert (await sheets.find_ancillary_player("@smaxhy")) is not None
    assert len(await sheets.find_all_player_rows("**Smaxhy**")) == 1


# ── full scan ────────────────────────────────────────────────────────

@pytest.fixture
def _db(fresh_db):
    yield


async def test_scan_credits_a_bold_formatted_log(monkeypatch, _db):
    tabs = install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", "Smaxhy​", "Jessica Cruz", "3", "1", "0"]]))
    msgs = [message(1, "**Username:** @Smaxhy\n**Lore name:** Jessica Cruz\n**EP:** 2\n**TP:** 1")]
    msgs[0].mentions = []
    result = await scan.run_scan(FakeChannel(msgs), FakeMember())
    assert result.players_not_found == 0, result.flagged_messages
    assert result.new_processed == 1
    rook = tabs["[D] ROOK"]
    assert (rook.cell(2, 8), rook.cell(2, 9)) == ("5", "2")


async def test_scan_not_found_message_suggests_closest_name(monkeypatch, _db):
    install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", "Smaxhy", "Jessica Cruz", "3", "1", "0"]]))
    msgs = [message(1, "Username: Smaxy\nEP: 2")]
    result = await scan.run_scan(FakeChannel(msgs), FakeMember())
    assert result.players_not_found == 1
    assert "closest on sheet: **Smaxhy**" in result.flagged_messages[-1]
