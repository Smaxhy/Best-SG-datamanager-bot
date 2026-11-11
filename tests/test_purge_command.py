"""Tests for /purge: it must delete the newest messages above the command, not the oldest."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from commands import purge


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


@pytest.fixture(autouse=True)
def _roles(monkeypatch):
    monkeypatch.setattr(purge.config, "DATA_MANAGER_ROLE_ID", 7)


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
        self.sent.append(content)


class FakeChannel:
    """Mimics discord.py ordering: oldest_first defaults to True when ``after`` is set."""

    name = "general"

    def __init__(self, messages):
        # messages: list of (id, created_at), any order
        self.messages = sorted(messages, key=lambda m: m[1])
        self.purge_kwargs = None
        self.deleted = []

    def permissions_for(self, member):
        return SimpleNamespace(manage_messages=True)

    async def purge(self, *, limit, before=None, after=None, oldest_first=None):
        self.purge_kwargs = dict(limit=limit, before=before, after=after, oldest_first=oldest_first)
        pool = [m for m in self.messages
                if (after is None or m[1] > after) and (before is None or m[1] < before)]
        if oldest_first is None:
            oldest_first = after is not None
        if not oldest_first:
            pool = list(reversed(pool))
        self.deleted = pool[:limit]
        return self.deleted


def _interaction(channel, created_at):
    return SimpleNamespace(
        guild=SimpleNamespace(me=object()),
        user=SimpleNamespace(id=42, roles=[SimpleNamespace(id=7)]),
        response=FakeResponse(),
        followup=FakeFollowup(),
        channel=channel,
        created_at=created_at,
    )


@pytest.fixture
def callback():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    purge.register(tree, client)
    return tree.get_command("purge").callback


async def test_purge_deletes_newest_messages_above_the_command(callback):
    now = datetime.now(timezone.utc)
    # ten messages, one per hour, the newest one minute before the command
    msgs = [(i, now - timedelta(hours=9 - i, minutes=1)) for i in range(10)]
    # one message posted after the command was typed — must survive
    msgs.append((99, now + timedelta(seconds=5)))
    channel = FakeChannel(msgs)
    inter = _interaction(channel, created_at=now)

    await callback(inter, amount=3)

    assert [m[0] for m in channel.deleted] == [9, 8, 7]
    assert "**3**" in inter.followup.sent[0]


async def test_purge_never_reaches_past_three_days(callback):
    now = datetime.now(timezone.utc)
    channel = FakeChannel([(1, now - timedelta(days=4)), (2, now - timedelta(hours=1))])
    inter = _interaction(channel, created_at=now)

    await callback(inter, amount=5)

    assert "**1**" in inter.followup.sent[0]


async def test_purge_rejects_out_of_range_amount(callback):
    inter = _interaction(FakeChannel([]), created_at=datetime.now(timezone.utc))
    await callback(inter, amount=21)
    assert "between 1 and 20" in inter.response.messages[0]
    assert inter.response.deferred is False
