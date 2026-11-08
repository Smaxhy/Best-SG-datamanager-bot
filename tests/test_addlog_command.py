"""Tests for the /addlog slash command: input clean-up, validation, duplicate handling."""

from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from commands import addlog
from models.player import PlayerRow
from services import sheets_service as sheets


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


@pytest.fixture(autouse=True)
def _roles(monkeypatch):
    monkeypatch.setattr(addlog.config, "DATA_MANAGER_ROLE_ID", 7)


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)

    async def defer(self, **kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content if content is not None else kwargs.get("embed"))


def _interaction():
    return SimpleNamespace(
        guild=object(),
        user=SimpleNamespace(id=42, roles=[SimpleNamespace(id=7)], mention="<@42>"),
        response=FakeResponse(),
        followup=FakeFollowup(),
        channel=SimpleNamespace(name="commands"),
    )


def _choice(name, value=None):
    return app_commands.Choice(name=name, value=value if value is not None else name)


ROOK = _choice("Rook", "[D] ROOK")
ANCILLARY = _choice("Ancillary", "[C] ANCILLARY")


@pytest.fixture
def callback():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    addlog.register(tree, client)
    return tree.get_command("addlog").callback


@pytest.fixture
def added(monkeypatch):
    calls = []

    async def fake_add(**kwargs):
        calls.append(kwargs)
        return f"**{kwargs['username']}** added"

    async def no_lookup(*args, **kwargs):
        raise AssertionError("duplicate check should happen inside add_player_to_sheet")

    monkeypatch.setattr(sheets, "add_player_to_sheet", fake_add)
    monkeypatch.setattr(sheets, "find_player", no_lookup)
    return calls


async def test_names_are_cleaned_up(callback, added):
    inter = _interaction()
    await callback(inter, "  Smaxhy ", " Jessica   Cruz ", ANCILLARY)
    assert added[0]["username"] == "Smaxhy"
    assert added[0]["lore_name"] == "Jessica Cruz"
    assert added[0]["subdivision"] is None and added[0]["rank"] == "RCT"
    assert isinstance(inter.followup.sent[0], discord.Embed)


async def test_blank_names_rejected(callback, added):
    inter = _interaction()
    await callback(inter, "   ", "Jess", ANCILLARY)
    assert "can't be blank" in inter.response.messages[0]
    assert added == []


async def test_subdivision_required_for_rook(callback, added):
    inter = _interaction()
    await callback(inter, "Smaxhy", "Jess", ROOK)
    assert "requires a subdivision" in inter.response.messages[0]
    assert added == []


async def test_subdivision_rejected_for_sheet_without_one(callback, added):
    inter = _interaction()
    await callback(inter, "Smaxhy", "Jess", ANCILLARY, subdivision=_choice("Charlie"))
    assert "doesn't have subdivisions" in inter.response.messages[0]
    assert added == []


async def test_wrong_subdivision_for_sheet(callback, added):
    inter = _interaction()
    await callback(inter, "Smaxhy", "Jess", ROOK, subdivision=_choice("Alpha"))
    assert "Rook only has Charlie or Delta" in inter.response.messages[0]
    assert added == []


async def test_rook_with_subdivision_and_rank(callback, added):
    inter = _interaction()
    await callback(inter, "Smaxhy", "Jess", ROOK, subdivision=_choice("Delta"), rank=_choice("PVT"))
    assert (added[0]["sheet_name"], added[0]["subdivision"], added[0]["rank"]) == ("[D] ROOK", "Delta", "PVT")


async def test_device_wielder_fields_passed(callback, added):
    inter = _interaction()
    await callback(inter, "Smaxhy", "Jess", _choice("Device Wielders", "DEVICE WIELDERS"),
                   device_status=_choice("Active"), sourced_by="NCO")
    assert added[0]["extra_fields"] == {"DEVICE STATUS": "Active", "SOURCED BY": "NCO"}


async def test_already_exists_message(callback, monkeypatch):
    async def exists(**kwargs):
        raise sheets.PlayerAlreadyExists(PlayerRow(
            sheet_name="[C] ANCILLARY", row_index=9, username="Smaxhy", lore_name="", rank="",
            ep=0, tp=0, mp=0,
        ))

    monkeypatch.setattr(sheets, "add_player_to_sheet", exists)
    inter = _interaction()
    await callback(inter, "Smaxhy", "Jess", ANCILLARY)
    assert inter.followup.sent == ["⚠️ **Smaxhy** already exists in **[C] ANCILLARY** (row 9)."]


async def test_other_errors_reported(callback, monkeypatch):
    async def full(**kwargs):
        raise RuntimeError("No empty row found in '[C] ANCILLARY'")

    monkeypatch.setattr(sheets, "add_player_to_sheet", full)
    inter = _interaction()
    await callback(inter, "Smaxhy", "Jess", ANCILLARY)
    assert inter.followup.sent == ["❌ Failed to add player: No empty row found in '[C] ANCILLARY'"]
