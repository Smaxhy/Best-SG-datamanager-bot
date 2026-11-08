"""Tests for the /scan slash command wrapper: per-channel locking, message splitting, expired interactions."""

import asyncio
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from commands import scan as scan_cmd
from models.player import ScanResult


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


@pytest.fixture(autouse=True)
def _roles(monkeypatch):
    monkeypatch.setattr(scan_cmd.config, "DATA_MANAGER_ROLE_ID", 7)
    monkeypatch.setattr(scan_cmd.config, "ALLOWED_CHANNEL_IDS", [])
    scan_cmd._scans_running.clear()


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)

    async def defer(self, **kwargs):
        self.deferred = True

    def is_done(self):
        return self.deferred or bool(self.messages)


class FakeFollowup:
    def __init__(self, error: Exception | None = None):
        self.sent = []
        self.error = error

    async def send(self, content=None, **kwargs):
        if self.error:
            raise self.error
        self.sent.append(content if content is not None else kwargs.get("embed"))


class FakeTextChannel:
    def __init__(self, channel_id=500, name="rook-logs"):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.sent = []

    def permissions_for(self, member):
        return SimpleNamespace(read_message_history=True)

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


def _interaction(followup_error=None):
    return SimpleNamespace(
        guild=SimpleNamespace(me=object()),
        user=SimpleNamespace(id=42, roles=[SimpleNamespace(id=7)], mention="<@42>"),
        response=FakeResponse(),
        followup=FakeFollowup(followup_error),
        channel=FakeTextChannel(1, "commands"),
    )


def _scan_callback():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    scan_cmd.register(tree, client)
    return tree.get_command("scan").callback


def _http_error(cls, status):
    resp = SimpleNamespace(status=status, reason="x")
    return cls(resp, "expired")


# ── chunk_lines ──────────────────────────────────────────────────────

def test_chunk_lines_fits_in_one_message():
    assert scan_cmd.chunk_lines("**Flagged:**", ["a", "b"]) == ["**Flagged:**\na\nb"]


def test_chunk_lines_splits_under_limit():
    lines = [f"⚠️ line {i} " + "x" * 180 for i in range(20)]
    chunks = scan_cmd.chunk_lines("**Flagged items:**", lines)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)
    assert chunks[0].startswith("**Flagged items:**")
    assert "\n".join(chunks).count("⚠️ line") == 20


def test_chunk_lines_truncates_a_single_huge_line():
    (chunk, second) = scan_cmd.chunk_lines("H", ["y" * 5000])
    assert chunk == "H"
    assert len(second) <= 2000 and second.endswith("…")


# ── _send ────────────────────────────────────────────────────────────

async def test_send_uses_followup():
    inter = _interaction()
    await scan_cmd._send(inter, "hi")
    assert inter.followup.sent == ["hi"]


@pytest.mark.parametrize("cls,status", [(discord.NotFound, 404), (discord.HTTPException, 401)])
async def test_send_falls_back_to_channel_when_interaction_expired(cls, status):
    inter = _interaction(_http_error(cls, status))
    await scan_cmd._send(inter, "done")
    assert inter.channel.sent == ["<@42> done"]


async def test_send_reraises_other_http_errors():
    inter = _interaction(_http_error(discord.HTTPException, 400))
    with pytest.raises(discord.HTTPException):
        await scan_cmd._send(inter, "bad")


# ── /scan command ────────────────────────────────────────────────────

async def test_second_scan_of_same_channel_is_refused(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_scan(channel, user, excluded_names=None, force=False):
        started.set()
        await release.wait()
        return ScanResult(channel_name=channel.name)

    monkeypatch.setattr(scan_cmd, "run_scan", slow_scan)
    callback = _scan_callback()
    channel = FakeTextChannel()

    first = asyncio.create_task(callback(_interaction(), channel))
    await started.wait()

    second = _interaction()
    await callback(second, channel)
    assert "already running" in second.response.messages[0]

    # A different channel can still be scanned meanwhile
    other = _interaction()
    other_task = asyncio.create_task(callback(other, FakeTextChannel(501, "bastion-logs")))
    await asyncio.sleep(0)
    assert other.response.deferred

    release.set()
    await asyncio.gather(first, other_task)
    assert scan_cmd._scans_running == set()

    # Once finished, the channel can be scanned again
    third = _interaction()
    await callback(third, channel)
    assert third.response.deferred and third.response.messages == []


async def test_channel_unlocked_even_if_scan_crashes(monkeypatch):
    async def broken_scan(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(scan_cmd, "run_scan", broken_scan)
    callback = _scan_callback()
    inter = _interaction()
    await callback(inter, FakeTextChannel())
    assert "Scan failed" in inter.followup.sent[0]
    assert scan_cmd._scans_running == set()


async def test_long_flag_list_is_split(monkeypatch):
    async def flagged_scan(channel, user, excluded_names=None, force=False):
        result = ScanResult(channel_name=channel.name)
        result.flagged_messages = [f"⚠️ Lore mismatch {i} " + "z" * 150 for i in range(25)]
        return result

    monkeypatch.setattr(scan_cmd, "run_scan", flagged_scan)
    callback = _scan_callback()
    inter = _interaction()
    await callback(inter, FakeTextChannel())
    embed, *texts = inter.followup.sent
    assert isinstance(embed, discord.Embed)
    assert len(texts) >= 2
    assert all(len(t) <= 2000 for t in texts)
    assert texts[-1].endswith("… and 5 more")


async def test_no_permission(monkeypatch):
    callback = _scan_callback()
    inter = _interaction()
    inter.user.roles = []
    await callback(inter, FakeTextChannel())
    assert "permission" in inter.response.messages[0]
    assert scan_cmd._scans_running == set()
