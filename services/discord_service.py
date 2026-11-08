"""
Core scan logic — called by the /scan slash command.
Orchestrates: message fetch → parse → sheet lookup → update → record.
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import discord

import config
from models.player import ParsedLog, ScanResult
from parser.log_parser import parse_log
from services import database_service as db
from services import sheets_service as sheets

logger = logging.getLogger("bot.scan")

# ── Blacklisted players (case-insensitive) ───────────────────────
BLACKLISTED_USERNAMES: set[str] = set()
BLACKLISTED_LORE_NAMES: set[str] = set()

# ── Max stats per log entry ──────────────────────────────────────
MAX_STAT_PER_LOG = 5


def _name_key(text: str) -> str:
    """Lowercase and drop accents, so "Förscher" and "Forscher" compare equal."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def lore_matches(log_lore: str, sheet_lore: str) -> bool:
    """
    Loose check that the lore name in a log belongs to the sheet's player.
    The username already matched, so this only catches clearly different
    names. Accepts spacing/accent differences, small typos, a middle name
    added or left out, first or last name only, and short forms (Vast → Vastku).
    """
    log_words = re.findall(r"\w+", _name_key(log_lore))
    sheet_words = re.findall(r"\w+", _name_key(sheet_lore))
    if not log_words or not sheet_words:
        return True
    log_c, sheet_c = "".join(log_words), "".join(sheet_words)
    if log_c == sheet_c:
        return True
    short, long = sorted((log_c, sheet_c), key=len)
    if len(short) >= 3 and long.startswith(short):
        return True
    if set(log_words) <= set(sheet_words) or set(sheet_words) <= set(log_words):
        return True
    return difflib.SequenceMatcher(None, log_c, sheet_c).ratio() >= 0.85


def _username_options(username: str) -> list[str]:
    """
    Names to try for a log's username. People sometimes paste their whole
    Discord nickname ("Lore Name | Username"), so try each part, username side first.
    """
    if "|" not in username:
        return [username]
    parts = [p.strip() for p in username.split("|") if p.strip()]
    return list(reversed(parts)) or [username]


def _extract_text(message: discord.Message) -> str:
    """
    Get all readable text from a message:
    - Regular message content
    - Embed descriptions
    - Embed field names and values
    """
    parts = []

    # Regular text content
    if message.content:
        parts.append(message.content)

    # Embeds (some bots post logs as embeds)
    for embed in message.embeds:
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            if field.name and field.value:
                parts.append(f"{field.name}: {field.value}")

    return "\n".join(parts)


async def run_scan(
    channel: discord.TextChannel,
    dm_user: discord.Member,
    excluded_names: set[str] | None = None,
    force: bool = False,
) -> ScanResult:
    """
    Scan *channel* for logs in the last SCAN_HOURS, update sheets,
    and return an aggregated ScanResult for the summary embed.
    """
    result = ScanResult(channel_name=channel.name)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.SCAN_HOURS)

    # Create audit record
    scan_id = await db.create_scan_record(
        dm_id=dm_user.id,
        dm_name=str(dm_user),
        channel_id=channel.id,
        channel_name=channel.name,
    )

    players_touched: set[str] = set()

    # Read each player tab once for the whole scan instead of once per log
    cache = sheets.SheetCache()
    for tab, exc in (await cache.preload()).items():
        result.errors += 1
        result.flagged_messages.append(
            f"⚠️ Could not read sheet **{tab}**: {exc} — players on it will show as not found."
        )

    # Logs that passed every check; their points are written in one batch at the end
    pending: list[tuple[discord.Message, ParsedLog, object]] = []

    async for message in channel.history(after=cutoff, oldest_first=True, limit=None):
        result.messages_checked += 1

        # Skip bot messages
        if message.author.bot:
            continue

        # Extract text from content + embeds
        full_text = _extract_text(message)
        if not full_text:
            continue

        parsed = parse_log(full_text)
        if parsed is None:
            continue

        logger.info("Parsed log: user='%s', lore='%s', EP=%d TP=%d MP=%d (msg %s)",
                    parsed.username, parsed.lore_name, parsed.ep, parsed.tp, parsed.mp, message.id)

        name_options = _username_options(parsed.username)
        option_keys = {n.strip().lower() for n in name_options}

        # ── Blacklist check ──────────────────────────────────────
        if option_keys & BLACKLISTED_USERNAMES:
            logger.info("  BLOCKED by username blacklist: '%s'", parsed.username)
            continue
        if parsed.lore_name and parsed.lore_name.strip().lower() in BLACKLISTED_LORE_NAMES:
            logger.info("  BLOCKED by lore blacklist: '%s'", parsed.lore_name)
            continue

        logger.info("  Passed blacklist check")

        # ── Exclude check (per-scan exclusion) ─────────────────
        if excluded_names:
            if option_keys & excluded_names:
                logger.info("  EXCLUDED by username: '%s'", parsed.username)
                result.flagged_messages.append(
                    f"⏭️ **{parsed.username}** excluded from this scan."
                )
                continue
            if parsed.lore_name and parsed.lore_name.strip().lower() in excluded_names:
                logger.info("  EXCLUDED by lore: '%s'", parsed.lore_name)
                result.flagged_messages.append(
                    f"⏭️ **{parsed.lore_name}** excluded from this scan."
                )
                continue

        logger.info("  Passed exclude check")

        # ── Stat limit check ─────────────────────────────────────
        over_limit = []
        if abs(parsed.ep) > MAX_STAT_PER_LOG:
            over_limit.append(f"EP: {parsed.ep}")
        if abs(parsed.tp) > MAX_STAT_PER_LOG:
            over_limit.append(f"TP: {parsed.tp}")
        if abs(parsed.mp) > MAX_STAT_PER_LOG:
            over_limit.append(f"MP: {parsed.mp}")
        if over_limit:
            result.invalid_logs += 1
            result.flagged_messages.append(
                f"🚫 **{parsed.username}** — Rejected: {', '.join(over_limit)} exceeds "
                f"the max of {MAX_STAT_PER_LOG} per log. Please alert an NCO."
            )
            logger.info("  BLOCKED by stat limit: %s", over_limit)
            continue

        logger.info("  Passed all checks — counting as valid log")
        result.valid_logs += 1

        # ── Duplicate check ──────────────────────────────────────
        if not force and await db.is_already_processed(message.id):
            result.already_processed += 1
            continue

        # ── Find player in sheets (pass lore name for disambiguation) ──
        try:
            player = None
            for name in name_options:
                player = await sheets.find_player(name, parsed.lore_name, cache=cache)
                if player is not None:
                    if name != parsed.username:
                        logger.info("  Matched '%s' using '%s'", parsed.username, name)
                        parsed.username = name
                    break
        except Exception as exc:
            logger.exception("Sheets error while looking up '%s'.", parsed.username)
            result.errors += 1
            result.flagged_messages.append(
                f"⚠️ Google Sheets error for **{parsed.username}**: {exc}"
            )
            continue

        if player is None:
            result.players_not_found += 1
            result.flagged_messages.append(
                f"❌ Player not found — Username: **{parsed.username}**"
            )
            continue

        # ── Ranklock check — skip stat updates for locked players ──
        lock = await db.is_ranklocked(player.username)
        if lock:
            result.flagged_messages.append(
                f"🔒 **{parsed.username}** is rank-locked"
                + (f" ({lock.reason})" if lock.reason else "")
                + " — EP/TP/MP skipped."
            )
            # Still record as processed so we don't flag the same message again
            try:
                await db.record_processed_log(
                    message_id=message.id,
                    channel_id=channel.id,
                    dm_id=dm_user.id,
                    dm_name=str(dm_user),
                    username=parsed.username,
                    lore_name=parsed.lore_name,
                    ep=0, tp=0, mp=0,
                    detachment=player.sheet_name,
                    status="skipped_ranklocked",
                )
            except Exception:
                pass
            continue

        # ── Training ban check — also skip stat updates ──
        tban = await db.is_trainingbanned(player.username)
        if tban:
            result.flagged_messages.append(
                f"🚫 **{parsed.username}** is training banned"
                + (f" ({tban.reason})" if tban.reason else "")
                + " — EP/TP/MP skipped."
            )
            try:
                await db.record_processed_log(
                    message_id=message.id,
                    channel_id=channel.id,
                    dm_id=dm_user.id,
                    dm_name=str(dm_user),
                    username=parsed.username,
                    lore_name=parsed.lore_name,
                    ep=0, tp=0, mp=0,
                    detachment=player.sheet_name,
                    status="skipped_trainingban",
                )
            except Exception:
                pass
            continue

        # ── Lore name validation (accepts partial/first-name matches) ──
        if parsed.lore_name and player.lore_name:
            lore_ok = lore_matches(parsed.lore_name, player.lore_name)
            if not lore_ok:
                result.lore_mismatches += 1
                result.flagged_messages.append(
                    f"⚠️ Lore mismatch for **{parsed.username}** — "
                    f"Sheet: *{player.lore_name}* / Log: *{parsed.lore_name}*"
                )
                continue

        # ── Validate rank (informational only) ───────────────────
        if player.rank and player.rank.upper() not in config.VALID_RANKS:
            logger.warning(
                "Player %s has unexpected rank '%s' in sheet '%s'.",
                player.username, player.rank, player.sheet_name,
            )

        pending.append((message, parsed, player))

    # ── Update Google Sheets (one read + one write per tab) ─────
    errors = await sheets.apply_stat_changes(
        [(player, parsed.ep, parsed.tp, parsed.mp) for _, parsed, player in pending]
    )

    for (message, parsed, player), exc in zip(pending, errors):
        if exc is not None:
            logger.error("Failed to update sheets for '%s': %s", parsed.username, exc)
            result.errors += 1
            result.flagged_messages.append(
                f"⚠️ Sheet update failed for **{parsed.username}**: {exc}"
            )
            continue

        # ── Record success (with crash protection) ───────────────
        try:
            await db.record_processed_log(
                message_id=message.id,
                channel_id=channel.id,
                dm_id=dm_user.id,
                dm_name=str(dm_user),
                username=parsed.username,
                lore_name=parsed.lore_name,
                ep=parsed.ep,
                tp=parsed.tp,
                mp=parsed.mp,
                detachment=player.sheet_name,
            )
        except Exception:
            logger.warning("Message %s already recorded — skipping.", message.id)
            result.already_processed += 1
            continue

        await db.increment_dm_score(dm_user.id, str(dm_user), logs=1)

        result.new_processed += 1
        result.ep_added += parsed.ep
        result.tp_added += parsed.tp
        result.mp_added += parsed.mp
        players_touched.add(parsed.username.lower())

    result.players_updated = len(players_touched)

    # Finalise scan audit
    await db.increment_dm_scans(dm_user.id, str(dm_user))
    await db.finish_scan_record(
        scan_id,
        messages_checked=result.messages_checked,
        logs_found=result.valid_logs,
        logs_processed=result.new_processed,
        already_processed=result.already_processed,
        players_updated=result.players_updated,
        players_not_found=result.players_not_found,
        invalid_logs=result.invalid_logs,
        lore_mismatches=result.lore_mismatches,
        errors=result.errors,
        ep_total=result.ep_added,
        tp_total=result.tp_added,
        mp_total=result.mp_added,
    )

    return result
