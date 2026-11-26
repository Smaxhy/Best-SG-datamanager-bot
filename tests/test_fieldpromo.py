"""/fieldpromo: promote without the point requirements — Staff/NCO only, points kept."""

from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from commands import fieldpromo
from services import database_service as db
from tests.fakes import ancillary_sheet, install_sheets, rook_sheet

STAFF, NCO, DM = 11, 12, 13


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


@pytest.fixture(autouse=True)
def _roles(monkeypatch):
    monkeypatch.setattr(fieldpromo.config, "STAFF_ROLE_ID", STAFF)
    monkeypatch.setattr(fieldpromo.config, "NCO_ROLE_ID", NCO)
    monkeypatch.setattr(fieldpromo.config, "DATA_MANAGER_ROLE_ID", DM)


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)

    async def defer(self, **kwargs):
        pass


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content if content is not None else kwargs.get("embed"))


def _interaction(role=STAFF):
    return SimpleNamespace(
        guild=object(),
        user=SimpleNamespace(id=42, roles=[SimpleNamespace(id=role)], mention="<@42>"),
        response=FakeResponse(),
        followup=FakeFollowup(),
        channel=SimpleNamespace(name="commands"),
    )


@pytest.fixture
def callback():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    fieldpromo.register(tree, client)
    return tree.get_command("fieldpromo").callback


def C(value, name=None):
    return app_commands.Choice(name=name or value, value=value)


async def test_field_promo_skips_requirements_and_keeps_points(callback, monkeypatch):
    ws = rook_sheet(charlie=[
        ["PVT", "Above", "A A", "9", "9", "9"],
        ["PVT", "Smaxhy", "Jessica Cruz", "1", "0", "0"],   # nowhere near 9 EP / 4 TP
        ["RCT", "Below", "B B", "0", "0", "0"],
    ])
    install_sheets(monkeypatch, ws)
    inter = _interaction(NCO)
    await callback(inter, "Smaxhy", C("PFC"), reason="Outstanding at the raid")

    embed = inter.followup.sent[-1]
    assert embed.title == "🎖️ Field Promotion"
    assert "**PVT** → **PFC**" in embed.description and "EP 1 · TP 0 · MP 0" in embed.description
    # Only Smaxhy's row changed: rank PFC, points untouched; nobody moved
    assert [ws.cell(r, 6) for r in (2, 3, 4)] == ["Above", "Smaxhy", "Below"]
    assert (ws.cell(3, 5), ws.cell(3, 8), ws.cell(3, 9)) == ("PFC", "1", "0")
    assert {c.row for call in ws.update_calls for c in call} == {4}


async def test_field_promo_into_detachment_keeps_all_points(callback, monkeypatch):
    tabs = install_sheets(
        monkeypatch,
        rook_sheet(),
        ancillary_sheet(players=[["RCT", "Newbie", "Nora Vance", "1", "0", "0", "", ""]]),
    )
    inter = _interaction(STAFF)
    await callback(inter, "Newbie", C("PVT"), C("[D] ROOK", "Rook"), C("Charlie"))

    rook, anc = tabs["[D] ROOK"], tabs["[C] ANCILLARY"]
    assert (rook.cell(2, 5), rook.cell(2, 6), rook.cell(2, 8)) == ("PVT", "Newbie", "1")
    assert anc.cell(2, 2) == ""  # removed from Ancillary
    assert "Rook / Charlie" in inter.followup.sent[-1].description


async def test_data_manager_alone_cannot_field_promote(callback, monkeypatch):
    install_sheets(monkeypatch, rook_sheet(charlie=[["PVT", "Smaxhy", "J C", "0", "0", "0"]]))
    inter = _interaction(DM)
    await callback(inter, "Smaxhy", C("PFC"))
    assert "Only Staff or NCOs" in inter.response.messages[0]


async def test_rank_lock_still_applies(callback, monkeypatch):
    ws = rook_sheet(charlie=[["PVT", "Smaxhy", "J C", "0", "0", "0"]])
    install_sheets(monkeypatch, ws)
    await db.ranklock_player("Smaxhy", 1, "Max", reason="on hold")
    inter = _interaction()
    await callback(inter, "Smaxhy", C("PFC"))
    assert "rank-locked" in inter.followup.sent[-1]
    assert ws.update_calls == []


async def test_same_rank_is_refused(callback, monkeypatch):
    ws = rook_sheet(charlie=[["PFC", "Smaxhy", "J C", "0", "0", "0"]])
    install_sheets(monkeypatch, ws)
    inter = _interaction()
    await callback(inter, "Smaxhy", C("PFC"))
    assert "already **PFC**" in inter.followup.sent[-1]
    assert ws.update_calls == []


async def test_bad_subdivision_combo(callback, monkeypatch):
    install_sheets(monkeypatch, rook_sheet())
    inter = _interaction()
    await callback(inter, "X", C("PVT"), C("[D] ROOK", "Rook"), C("Alpha"))
    assert "doesn't belong" in inter.response.messages[0]


async def test_normal_promote_and_transfer_still_enforce_requirements(monkeypatch):
    from services import sheets_service as sheets

    tabs = install_sheets(
        monkeypatch,
        rook_sheet(charlie=[["PVT", "Smaxhy", "J C", "1", "0", "0"]]),
        ancillary_sheet(players=[["RCT", "Newbie", "Nora Vance", "1", "0", "0", "", ""]]),
    )
    with pytest.raises(ValueError, match="does not meet requirements"):
        await sheets.promote_rank(await sheets.find_player("Smaxhy"), "PFC")
    with pytest.raises(ValueError, match="does not meet requirements"):
        await sheets.transfer_player(await sheets.find_player("Newbie"), "[D] ROOK", "Delta", "PVT")
    assert all(ws.update_calls == [] for ws in tabs.values())
