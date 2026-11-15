"""/fix: turn scan problems into sheet fixes, apply only what's ticked, then re-scan."""

from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from commands import fix as fix_cmd
from commands import scan as scan_cmd
from services import discord_service as scan
from services import fix_service
from tests.fakes import FakeChannel, FakeMember, ancillary_sheet, install_sheets, message, rook_sheet


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


@pytest.fixture(autouse=True)
def _roles(monkeypatch):
    monkeypatch.setattr(scan_cmd.config, "DATA_MANAGER_ROLE_ID", 7)
    monkeypatch.setattr(scan_cmd.config, "ALLOWED_CHANNEL_IDS", [])
    scan_cmd._scans_running.clear()


def _tabs(monkeypatch, rook=(), anc=()):
    return install_sheets(monkeypatch, rook_sheet(charlie=list(rook)), ancillary_sheet(players=list(anc)))


def _usernames(ws, col):
    return [ws.cell(r, col) for r in range(2, len(ws.values)) if ws.cell(r, col)]


async def _plan(monkeypatch, messages):
    result = await scan.run_scan(FakeChannel(messages), FakeMember())
    return result, await fix_service.build_plan(result.issues)


# ── planning ─────────────────────────────────────────────────────────

async def test_new_player_is_planned_for_ancillary_once_per_player(monkeypatch):
    _tabs(monkeypatch, rook=[["PVT", "Smaxhy", "Jessica Cruz", "0", "0", "0"]])
    _, plan = await _plan(monkeypatch, [
        message(1, "Username: Newbie\nLore name: Nora Vance\nEP: 1"),
        message(2, "Username: newbie\nLore name: Nora Vance\nTP: 2"),
    ])
    assert [(f.kind, f.username, f.lore_name, f.logs) for f in plan.fixes] == [("add", "Newbie", "Nora Vance", 2)]
    assert plan.review == []


async def test_likely_typo_is_not_added(monkeypatch):
    _tabs(monkeypatch, rook=[["PVT", "Smaxhy", "Jessica Cruz", "0", "0", "0"]])
    _, plan = await _plan(monkeypatch, [message(1, "Username: Smaxy\nLore name: Nora Vance\nEP: 1")])
    assert plan.fixes == []
    assert "typo of **Smaxhy**" in plan.review[0]


async def test_missing_lore_name_needs_a_human(monkeypatch):
    _tabs(monkeypatch)
    _, plan = await _plan(monkeypatch, [message(1, "Username: Newbie\nEP: 1")])
    assert plan.fixes == []
    assert "no lore name" in plan.review[0]


async def test_changed_username_is_planned_as_rename(monkeypatch):
    _tabs(monkeypatch, rook=[["PVT", "OldName", "Jessica Cruz", "4", "0", "0"]])
    _, plan = await _plan(monkeypatch, [message(1, "Username: NewName\nLore name: Jessica Cruz\nEP: 1")])
    (f,) = plan.fixes
    assert (f.kind, f.old_value, f.username) == ("rename", "OldName", "NewName")


async def test_lore_shared_by_two_players_is_not_renamed(monkeypatch):
    _tabs(monkeypatch, rook=[
        ["PVT", "One", "Jessica Cruz", "0", "0", "0"],
        ["PVT", "Two", "Jessica Cruz", "0", "0", "0"],
    ])
    _, plan = await _plan(monkeypatch, [message(1, "Username: Brandnew\nLore name: Jessica Cruz\nEP: 1")])
    assert plan.fixes == []
    assert "several players" in plan.review[0]


async def test_wrong_lore_is_planned_as_lore_update(monkeypatch):
    _tabs(monkeypatch, rook=[["PVT", "Smaxhy", "Jessica Cruz", "0", "0", "0"]])
    result, plan = await _plan(monkeypatch, [message(1, "Username: Smaxhy\nLore name: Bob Smith\nEP: 1")])
    assert result.lore_mismatches == 1
    (f,) = plan.fixes
    assert (f.kind, f.username, f.old_value, f.lore_name) == ("lore", "Smaxhy", "Jessica Cruz", "Bob Smith")


async def test_conflicting_lore_in_logs_needs_a_human(monkeypatch):
    _tabs(monkeypatch, rook=[["PVT", "Smaxhy", "Jessica Cruz", "0", "0", "0"]])
    _, plan = await _plan(monkeypatch, [
        message(1, "Username: Smaxhy\nLore name: Bob Smith\nEP: 1"),
        message(2, "Username: Smaxhy\nLore name: Tim Brown\nEP: 1"),
    ])
    assert plan.fixes == []
    assert "different lore names" in plan.review[0]


# ── applying ─────────────────────────────────────────────────────────

async def test_apply_writes_each_kind_of_fix(monkeypatch):
    tabs = _tabs(monkeypatch, rook=[
        ["PVT", "OldName", "Jessica Cruz", "4", "0", "0"],
        ["PVT", "Smaxhy", "Wrong Lore", "0", "0", "0"],
    ])
    _, plan = await _plan(monkeypatch, [
        message(1, "Username: NewName\nLore name: Jessica Cruz\nEP: 1"),
        message(2, "Username: Smaxhy\nLore name: Bob Smith\nEP: 1"),
        message(3, "Username: Newbie\nLore name: Nora Vance\nEP: 1"),
    ])
    done, failed = await fix_service.apply_fixes(plan.fixes)
    assert failed == [] and len(done) == 3

    rook, anc = tabs["[D] ROOK"], tabs["[C] ANCILLARY"]
    assert (rook.cell(2, 6), rook.cell(2, 7)) == ("NewName", "Jessica Cruz")
    assert (rook.cell(3, 6), rook.cell(3, 7)) == ("Smaxhy", "Bob Smith")
    assert (anc.cell(2, 1), anc.cell(2, 2), anc.cell(2, 3)) == ("RCT", "Newbie", "Nora Vance")

    # Every log now goes through
    result = await scan.run_scan(FakeChannel([
        message(1, "Username: NewName\nLore name: Jessica Cruz\nEP: 1"),
        message(2, "Username: Smaxhy\nLore name: Bob Smith\nEP: 1"),
        message(3, "Username: Newbie\nLore name: Nora Vance\nEP: 1"),
    ]), FakeMember())
    assert (result.new_processed, result.players_not_found, result.lore_mismatches) == (3, 0, 0)
    assert rook.cell(2, 8) == "5"


# ── the slash command ────────────────────────────────────────────────

class FakeResponse:
    def __init__(self):
        self.messages, self.deferred, self.edits = [], False, []

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)

    async def defer(self, **kwargs):
        self.deferred = True

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    def is_done(self):
        return self.deferred or bool(self.messages)


class FakeFollowup:
    def __init__(self):
        self.sent, self.views = [], []

    async def send(self, content=None, *, view=None, wait=False, **kwargs):
        self.sent.append(content)
        self.views.append(view)
        return SimpleNamespace(content=content, edit=None)


class FakeTextChannel(FakeChannel):
    def __init__(self, messages):
        super().__init__(messages)
        self.mention = "<#500>"

    def permissions_for(self, member):
        return SimpleNamespace(read_message_history=True)


def _interaction(user_id=42):
    return SimpleNamespace(
        guild=SimpleNamespace(me=object()),
        user=SimpleNamespace(id=user_id, roles=[SimpleNamespace(id=7)], mention=f"<@{user_id}>"),
        response=FakeResponse(),
        followup=FakeFollowup(),
        channel=SimpleNamespace(name="commands"),
        message=SimpleNamespace(content="plan"),
    )


def _fix_callback():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    fix_cmd.register(tree, client)
    return tree.get_command("fix").callback


LOGS = [
    message(1, "Username: Newbie\nLore name: Nora Vance\nEP: 2"),
    message(2, "Username: Other\nLore name: Olly Ray\nTP: 1"),
]


async def test_fix_command_applies_only_ticked_fixes_then_rescans(monkeypatch):
    tabs = _tabs(monkeypatch)
    channel = FakeTextChannel(LOGS)
    inter = _interaction()
    await _fix_callback()(inter, channel)

    view = inter.followup.views[-1]
    assert isinstance(view, fix_cmd.FixView)
    assert "Add **Newbie**" in inter.followup.sent[-1] and "Add **Other**" in inter.followup.sent[-1]

    # Untick "Other", then Apply
    view.select._values = ["0"]  # what discord.py fills in from the user's choice
    pick = _interaction()
    await view._on_select(pick)
    press = _interaction()
    await view.apply.callback(press)

    anc = tabs["[C] ANCILLARY"]
    assert _usernames(anc, 2) == ["Newbie"]
    assert anc.cell(2, 4) == "2"  # re-scan credited Newbie's EP
    assert any("Fix results" in m and "1 new logs credited" in m for m in press.followup.sent)
    assert not scan_cmd._scans_running


async def test_only_the_invoker_can_press_apply(monkeypatch):
    _tabs(monkeypatch)
    inter = _interaction(42)
    await _fix_callback()(inter, FakeTextChannel(LOGS))
    view = inter.followup.views[-1]
    intruder = _interaction(99)
    assert await view.interaction_check(intruder) is False
    assert "Only the person" in intruder.response.messages[0]


async def test_cancel_changes_nothing(monkeypatch):
    tabs = _tabs(monkeypatch)
    inter = _interaction()
    await _fix_callback()(inter, FakeTextChannel(LOGS))
    press = _interaction()
    await inter.followup.views[-1].cancel.callback(press)
    assert _usernames(tabs["[C] ANCILLARY"], 2) == []
    assert "Cancelled" in press.response.edits[0]["content"]


async def test_nothing_to_fix(monkeypatch):
    _tabs(monkeypatch, rook=[["PVT", "Smaxhy", "Jessica Cruz", "0", "0", "0"]])
    inter = _interaction()
    await _fix_callback()(inter, FakeTextChannel([message(1, "Username: Smaxhy\nEP: 1")]))
    assert "Nothing to fix" in inter.followup.sent[-1]
    assert inter.followup.views[-1] is None


async def test_fix_refuses_while_channel_is_being_scanned(monkeypatch):
    _tabs(monkeypatch)
    channel = FakeTextChannel(LOGS)
    scan_cmd._scans_running.add(channel.id)
    inter = _interaction()
    await _fix_callback()(inter, channel)
    assert "already running" in inter.response.messages[0]
