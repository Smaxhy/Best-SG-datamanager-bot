"""Tests for bot-wide behaviour: the slash-command error handler and one-time command cleanup."""

from types import SimpleNamespace

import pytest
from discord import app_commands

import bot


class FakeResponse:
    def __init__(self, done: bool):
        self._done = done
        self.sent = []

    def is_done(self):
        return self._done

    async def send_message(self, content, **kwargs):
        self.sent.append((content, kwargs))


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


def _interaction(done: bool):
    return SimpleNamespace(
        command=SimpleNamespace(name="promote"),
        response=FakeResponse(done),
        followup=FakeFollowup(),
    )


@pytest.fixture
def client():
    return bot.DataManagerBot()


async def test_error_before_reply_sends_message(client):
    inter = _interaction(done=False)
    await client.on_app_command_error(inter, app_commands.AppCommandError("boom"))
    (content, kwargs), = inter.response.sent
    assert "Something went wrong" in content and kwargs["ephemeral"] is True


async def test_error_after_defer_uses_followup(client):
    inter = _interaction(done=True)
    await client.on_app_command_error(inter, app_commands.AppCommandError("boom"))
    (content, _), = inter.followup.sent
    assert "Something went wrong" in content
    assert inter.response.sent == []


async def test_check_failure_message(client):
    inter = _interaction(done=False)
    await client.on_app_command_error(inter, app_commands.CheckFailure())
    assert "can't use this command" in inter.response.sent[0][0]


def test_error_handler_is_installed(client):
    assert client.tree.on_error == client.on_app_command_error


async def test_guild_commands_cleared_only_once(client, monkeypatch):
    synced = []
    guild = SimpleNamespace(name="SG")
    monkeypatch.setattr(type(client), "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(type(client), "user", property(lambda self: SimpleNamespace(id=1, __str__=lambda s: "bot")))
    monkeypatch.setattr(client.tree, "clear_commands", lambda guild: None)

    async def fake_sync(guild=None):
        synced.append(guild)

    monkeypatch.setattr(client.tree, "sync", fake_sync)
    await client.on_ready()
    await client.on_ready()  # reconnect
    assert synced == [guild]
