"""/promote must honour a rank lock even when the player is looked up by lore name."""

from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from commands import promote
from models.player import PlayerRow
from services import database_service as db
from services import sheets_service as sheets


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


@pytest.fixture(autouse=True)
def _roles(monkeypatch):
    monkeypatch.setattr(promote.config, "DATA_MANAGER_ROLE_ID", 7)


class FakeResponse:
    async def send_message(self, content=None, **kwargs):
        raise AssertionError(f"unexpected early response: {content}")

    async def defer(self, **kwargs):
        pass


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


def _interaction():
    return SimpleNamespace(
        guild=object(),
        user=SimpleNamespace(id=42, roles=[SimpleNamespace(id=7)]),
        response=FakeResponse(),
        followup=FakeFollowup(),
        channel=SimpleNamespace(name="commands"),
    )


@pytest.fixture
def callback():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    promote.register(tree, client)
    return tree.get_command("promote").callback


async def test_ranklock_applies_when_promoting_by_lore_first_name(callback, monkeypatch):
    smaxhy = PlayerRow(sheet_name="[D] ROOK", row_index=5, username="Smaxhy",
                       lore_name="Jessica Hale", rank="PVT", ep=20, tp=20, mp=0)

    async def find_player(name, *a, **k):
        return smaxhy if name.lower() in ("smaxhy", "jessica") else None

    async def must_not_promote(*a, **k):
        raise AssertionError("rank-locked player was promoted")

    monkeypatch.setattr(sheets, "find_player", find_player)
    monkeypatch.setattr(sheets, "promote_player", must_not_promote, raising=False)
    monkeypatch.setattr(sheets, "transfer_player", must_not_promote, raising=False)
    await db.ranklock_player("Smaxhy", 1, "Max", reason="on hold")

    inter = _interaction()
    await callback(inter, "Jessica", app_commands.Choice(name="PFC", value="PFC"))

    assert len(inter.followup.sent) == 1
    assert "rank-locked" in inter.followup.sent[0]
    assert "Smaxhy" in inter.followup.sent[0]
