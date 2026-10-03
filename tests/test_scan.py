"""Tests for the /scan pipeline (run_scan) with a fake channel, fake Sheets, and the real in-memory DB."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from models import ProcessedLog, ScanHistory
from models.player import PlayerRow
from services import database_service as db
from services import discord_service as scan
from services import sheets_service as sheets
from tests.fakes import FakeChannel, FakeMember, embed, message


@pytest.fixture(autouse=True)
def _db(fresh_db):
    yield


def _row(username="Bob", lore="Bob Jones", rank="PVT", sheet="[D] ROOK", ep=0, tp=0, mp=0) -> PlayerRow:
    return PlayerRow(sheet_name=sheet, row_index=3, username=username, lore_name=lore, rank=rank,
                     ep=ep, tp=tp, mp=mp, col_ep=8, col_tp=9, col_mp=10)


class FakeSheets:
    """Records stat updates; looks players up from a dict keyed by lowercase username."""

    def __init__(self, monkeypatch, players=()):
        self.players = {p.username.lower(): p for p in players}
        self.updates: list[tuple] = []
        self.find_error: Exception | None = None
        self.update_error: Exception | None = None
        monkeypatch.setattr(sheets, "find_player", self.find_player)
        monkeypatch.setattr(sheets, "update_player_stats", self.update_player_stats)

    async def find_player(self, username, lore_name=None):
        if self.find_error:
            raise self.find_error
        return self.players.get(username.strip().lower())

    async def update_player_stats(self, player, *, ep_delta, tp_delta, mp_delta):
        if self.update_error:
            raise self.update_error
        self.updates.append((player.username, ep_delta, tp_delta, mp_delta))


async def _processed_rows() -> list[ProcessedLog]:
    async with db.async_session() as session:
        return list((await session.execute(select(ProcessedLog))).scalars().all())


async def _run(messages, **kwargs):
    return await scan.run_scan(FakeChannel(messages), FakeMember(), **kwargs)


LOG = "Username: Bob\nLore name: Bob Jones\nEP: 2\nTP: 1"


# ── _extract_text ────────────────────────────────────────────────────

def test_extract_text_combines_content_and_embeds():
    msg = message(1, "hello", embeds=[embed("desc", fields=[("EP", "2"), ("Empty", ""), ("TP", "1")])])
    assert scan._extract_text(msg) == "hello\ndesc\nEP: 2\nTP: 1"


def test_extract_text_empty():
    assert scan._extract_text(message(1, "", embeds=[embed(None)])) == ""


# ── Happy path ───────────────────────────────────────────────────────

async def test_valid_log_updates_sheet_and_records_everything(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    channel = FakeChannel([message(1, LOG)])
    result = await scan.run_scan(channel, FakeMember(42, "Max"))

    assert fake.updates == [("Bob", 2, 1, 0)]
    assert (result.messages_checked, result.valid_logs, result.new_processed) == (1, 1, 1)
    assert (result.ep_added, result.tp_added, result.mp_added, result.players_updated) == (2, 1, 0, 1)
    assert result.flagged_messages == []

    (row,) = await _processed_rows()
    assert (row.discord_message_id, row.username, row.ep_change, row.status) == (1, "Bob", 2, "success")
    week = await db.get_dm_current_week(42)
    assert (week.logs_processed, week.scans_completed) == (1, 1)

    async with db.async_session() as session:
        rec = (await session.execute(select(ScanHistory))).scalar_one()
    assert (rec.messages_checked, rec.logs_processed, rec.ep_total) == (1, 1, 2)
    assert rec.scan_finished is not None


async def test_history_window_uses_scan_hours(monkeypatch):
    FakeSheets(monkeypatch)
    monkeypatch.setattr(scan.config, "SCAN_HOURS", 10)
    channel = FakeChannel([])
    await scan.run_scan(channel, FakeMember())
    after = channel.history_kwargs["after"]
    expected = datetime.now(timezone.utc) - timedelta(hours=10)
    assert abs((after - expected).total_seconds()) < 5
    assert channel.history_kwargs["oldest_first"] is True


async def test_log_posted_as_embed_is_processed(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    await _run([message(1, "", embeds=[embed(LOG)])])
    assert fake.updates == [("Bob", 2, 1, 0)]


async def test_players_updated_counts_unique_players(monkeypatch):
    FakeSheets(monkeypatch, [_row(), _row("Al", "Al Smith")])
    result = await _run([
        message(1, LOG),
        message(2, LOG),
        message(3, "Username: Al\nEP: 1"),
    ])
    assert (result.new_processed, result.players_updated, result.ep_added) == (3, 2, 5)


# ── Messages that are skipped silently ───────────────────────────────

async def test_bot_and_non_log_messages_are_ignored(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    result = await _run([
        message(1, LOG, bot=True),
        message(2, "gg everyone"),
        message(3, ""),
    ])
    assert result.messages_checked == 3
    assert result.valid_logs == 0
    assert fake.updates == []


async def test_blacklist_blocks_username_and_lore(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row(), _row("Al", "Al Smith")])
    monkeypatch.setattr(scan, "BLACKLISTED_USERNAMES", {"bob"})
    monkeypatch.setattr(scan, "BLACKLISTED_LORE_NAMES", {"al smith"})
    result = await _run([message(1, LOG), message(2, "Username: Al\nLore name: Al Smith\nEP: 1")])
    assert result.valid_logs == 0
    assert fake.updates == []


# ── Flagged / rejected logs ──────────────────────────────────────────

async def test_excluded_names_are_flagged(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row(), _row("Al", "Al Smith")])
    result = await _run(
        [message(1, LOG), message(2, "Username: Al\nLore name: Al Smith\nEP: 1")],
        excluded_names={"bob", "al smith"},
    )
    assert fake.updates == []
    assert result.flagged_messages == [
        "⏭️ **Bob** excluded from this scan.",
        "⏭️ **Al Smith** excluded from this scan.",
    ]


@pytest.mark.parametrize("stats,flag", [
    ("EP: 6", "EP: 6"),
    ("TP: -6", "TP: -6"),
    ("EP: 1\nMP: 9", "MP: 9"),
])
async def test_stats_over_limit_are_rejected(monkeypatch, stats, flag):
    fake = FakeSheets(monkeypatch, [_row()])
    result = await _run([message(1, f"Username: Bob\n{stats}")])
    assert result.invalid_logs == 1 and result.valid_logs == 0
    assert flag in result.flagged_messages[0]
    assert fake.updates == []


async def test_stat_exactly_at_limit_is_accepted(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    await _run([message(1, "Username: Bob\nEP: 5\nTP: -5\nMP: 5")])
    assert fake.updates == [("Bob", 5, -5, 5)]


async def test_player_not_found(monkeypatch):
    FakeSheets(monkeypatch)
    result = await _run([message(1, LOG)])
    assert result.players_not_found == 1
    assert result.flagged_messages == ["❌ Player not found — Username: **Bob**"]
    assert await db.is_already_processed(1) is False


async def test_sheets_lookup_error_is_flagged_and_scan_continues(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    fake.find_error = RuntimeError("quota")
    result = await _run([message(1, LOG), message(2, LOG)])
    assert result.errors == 2
    assert "quota" in result.flagged_messages[0]
    assert await db.is_already_processed(1) is False


async def test_sheet_update_error_does_not_record_message(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    fake.update_error = RuntimeError("no stat columns")
    result = await _run([message(1, LOG)])
    assert result.errors == 1 and result.new_processed == 0
    assert await db.is_already_processed(1) is False
    assert await db.get_dm_current_week(42) is not None  # scan still counted


# ── Duplicates ───────────────────────────────────────────────────────

async def test_already_processed_messages_are_skipped(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    await _run([message(1, LOG)])
    result = await _run([message(1, LOG)])
    assert result.already_processed == 1 and result.new_processed == 0
    assert len(fake.updates) == 1


async def test_force_reapplies_but_does_not_double_record(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    await _run([message(1, LOG)])
    result = await _run([message(1, LOG)], force=True)
    # Sheet is updated again, the DB insert hits the unique constraint and is counted as already processed
    assert len(fake.updates) == 2
    assert result.already_processed == 1 and result.new_processed == 0
    assert len(await _processed_rows()) == 1


# ── Rank lock / training ban ─────────────────────────────────────────

async def test_ranklocked_player_is_skipped_and_recorded(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    await db.ranklock_player("Bob", 1, "NCO", reason="pending review")
    result = await _run([message(1, LOG)])
    assert fake.updates == []
    assert result.flagged_messages == ["🔒 **Bob** is rank-locked (pending review) — EP/TP/MP skipped."]
    (row,) = await _processed_rows()
    assert (row.status, row.ep_change) == ("skipped_ranklocked", 0)


async def test_timed_ranklock_blocks_scan(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    await db.ranklock_player("Bob", 1, "NCO", hours=1)
    await _run([message(1, LOG)])
    assert fake.updates == []


async def test_trainingbanned_player_is_skipped_and_recorded(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row()])
    await db.trainingban_player("Bob", 1, "NCO")
    result = await _run([message(1, LOG)])
    assert fake.updates == []
    assert result.flagged_messages == ["🚫 **Bob** is training banned — EP/TP/MP skipped."]
    (row,) = await _processed_rows()
    assert row.status == "skipped_trainingban"


# ── Lore validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("log_lore", ["Bob Jones", "bob jones", "Bob", "  BOB "])
async def test_lore_name_matches_are_accepted(monkeypatch, log_lore):
    fake = FakeSheets(monkeypatch, [_row(lore="Bob Jones")])
    result = await _run([message(1, f"Username: Bob\nLore name: {log_lore}\nEP: 1")])
    assert result.lore_mismatches == 0
    assert len(fake.updates) == 1


@pytest.mark.parametrize("log_lore", ["Jones", "Bobby", "Bob Smith"])
async def test_lore_name_mismatch_is_flagged(monkeypatch, log_lore):
    fake = FakeSheets(monkeypatch, [_row(lore="Bob Jones")])
    result = await _run([message(1, f"Username: Bob\nLore name: {log_lore}\nEP: 1")])
    assert result.lore_mismatches == 1
    assert fake.updates == []
    assert "Lore mismatch for **Bob**" in result.flagged_messages[0]


async def test_lore_check_skipped_when_sheet_has_no_lore(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row(lore="")])
    await _run([message(1, "Username: Bob\nLore name: Anything\nEP: 1")])
    assert len(fake.updates) == 1


async def test_unexpected_rank_is_informational_only(monkeypatch):
    fake = FakeSheets(monkeypatch, [_row(rank="SGT")])
    await _run([message(1, LOG)])
    assert len(fake.updates) == 1
