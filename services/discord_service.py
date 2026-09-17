"""
Core scan logic — called by the /scan slash command.

Orchestrates:

message fetch
    ↓
log parser
    ↓
username/lore/detachment lookup
    ↓
Google Sheets update
    ↓
database record
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord

import config
from models.player import ScanResult
from parser.log_parser import parse_log
from services import database_service as db
from services import sheets_service as sheets


logger = logging.getLogger("bot.scan")


# ─────────────────────────────────────────────────────────────
# Extract Discord message text
# ─────────────────────────────────────────────────────────────

def _extract_text(
    message: discord.Message,
) -> str:

    parts: list[str] = []

    # Normal message content.
    if message.content:
        parts.append(
            message.content
        )

    # Embed descriptions + fields.
    for embed in message.embeds:

        if embed.description:
            parts.append(
                embed.description
            )

        for field in embed.fields:

            if field.name and field.value:

                parts.append(
                    f"{field.name}: {field.value}"
                )

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# Scan
# ─────────────────────────────────────────────────────────────

async def run_scan(
    channel: discord.TextChannel,
    dm_user: discord.Member,
) -> ScanResult:

    result = ScanResult(
        channel_name=channel.name
    )

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=config.SCAN_HOURS
        )
    )

    # ─────────────────────────────────────────────────────────
    # Create audit record
    # ─────────────────────────────────────────────────────────

    scan_id = await db.create_scan_record(
        dm_id=dm_user.id,
        dm_name=str(dm_user),
        channel_id=channel.id,
        channel_name=channel.name,
    )

    players_touched: set[str] = set()

    # ─────────────────────────────────────────────────────────
    # Read Discord history
    # ─────────────────────────────────────────────────────────

    async for message in channel.history(
        after=cutoff,
        oldest_first=True,
        limit=None,
    ):

        result.messages_checked += 1

        # Ignore bot messages.
        if message.author.bot:
            continue

        full_text = _extract_text(
            message
        )

        if not full_text:
            continue

        # ─────────────────────────────────────────────────────
        # Parse
        # ─────────────────────────────────────────────────────

        parsed = parse_log(
            full_text
        )

        if parsed is None:
            continue

        result.valid_logs += 1

        logger.info(
            "Parsed log: username='%s', "
            "lore='%s', detachment='%s', "
            "EP=%d TP=%d MP=%d",
            parsed.username,
            parsed.lore_name or "",
            parsed.detachment or "",
            parsed.ep,
            parsed.tp,
            parsed.mp,
        )

        # ─────────────────────────────────────────────────────
        # Duplicate Discord message check
        # ─────────────────────────────────────────────────────

        if await db.is_already_processed(
            message.id
        ):

            result.already_processed += 1

            continue

        # ─────────────────────────────────────────────────────
        # Find player
        # ─────────────────────────────────────────────────────

        try:

            player = await sheets.find_player(
                parsed.username,
                parsed.lore_name,
                parsed.detachment,
            )

        except Exception as exc:

            logger.exception(
                "Sheets error while looking up '%s'.",
                parsed.username,
            )

            result.errors += 1

            result.flagged_messages.append(
                f"⚠️ Google Sheets error for "
                f"**{parsed.username}**: {exc}"
            )

            continue

        # ─────────────────────────────────────────────────────
        # No player
        # ─────────────────────────────────────────────────────

        if player is None:

            result.players_not_found += 1

            if parsed.detachment:

                result.flagged_messages.append(
                    f"❌ Player not found or ambiguous — "
                    f"Username: **{parsed.username}** "
                    f"| Detachment: **{parsed.detachment}**"
                )

            else:

                result.flagged_messages.append(
                    f"❌ Player not found or ambiguous — "
                    f"Username: **{parsed.username}**"
                )

            continue

        # ─────────────────────────────────────────────────────
        # Lore validation
        #
        # Only validate if the parser actually found a lore name.
        #
        # Detachment/rank metadata does NOT reach this section.
        # ─────────────────────────────────────────────────────

        if (
            parsed.lore_name
            and player.lore_name
        ):

            log_lore = (
                parsed.lore_name
                .strip()
                .casefold()
            )

            sheet_lore = (
                player.lore_name
                .strip()
                .casefold()
            )

            # Exact full lore match.
            #
            # Also allow exact first-name matching when the user
            # genuinely entered a character name such as:
            #
            # Jessica
            #
            # This is ONLY validation after username lookup.
            # It is NOT used to find a player.
            sheet_parts = sheet_lore.split()

            lore_ok = (
                log_lore == sheet_lore
                or (
                    bool(sheet_parts)
                    and log_lore == sheet_parts[0]
                )
            )

            if not lore_ok:

                result.lore_mismatches += 1

                result.flagged_messages.append(
                    f"⚠️ Lore mismatch for "
                    f"**{parsed.username}** — "
                    f"Sheet: *{player.lore_name}* / "
                    f"Log: *{parsed.lore_name}*"
                )

                logger.warning(
                    "Lore mismatch: username='%s', "
                    "sheet='%s', log='%s'",
                    parsed.username,
                    player.lore_name,
                    parsed.lore_name,
                )

                continue

        # ─────────────────────────────────────────────────────
        # Validate sheet rank
        # ─────────────────────────────────────────────────────

        if (
            player.rank
            and player.rank.upper()
            not in config.VALID_RANKS
        ):

            logger.warning(
                "Player %s has unexpected rank "
                "'%s' in sheet '%s'.",
                player.username,
                player.rank,
                player.sheet_name,
            )

        # ─────────────────────────────────────────────────────
        # Update Google Sheets
        # ─────────────────────────────────────────────────────

        try:

            await sheets.update_player_stats(
                player,
                ep_delta=parsed.ep,
                tp_delta=parsed.tp,
                mp_delta=parsed.mp,
            )

        except Exception as exc:

            logger.exception(
                "Failed to update sheets "
                "for '%s'.",
                parsed.username,
            )

            result.errors += 1

            result.flagged_messages.append(
                f"⚠️ Sheet update failed for "
                f"**{parsed.username}**: {exc}"
            )

            continue

        # ─────────────────────────────────────────────────────
        # Record processed log
        # ─────────────────────────────────────────────────────

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

            logger.warning(
                "Message %s already recorded — "
                "skipping.",
                message.id,
            )

            result.already_processed += 1

            continue

        # ─────────────────────────────────────────────────────
        # Update DM score
        # ─────────────────────────────────────────────────────

        await db.increment_dm_score(
            dm_user.id,
            str(dm_user),
            logs=1,
        )

        # ─────────────────────────────────────────────────────
        # Update scan totals
        # ─────────────────────────────────────────────────────

        result.new_processed += 1

        result.ep_added += parsed.ep

        result.tp_added += parsed.tp

        result.mp_added += parsed.mp

        players_touched.add(
            parsed.username.casefold()
        )

        logger.info(
            "SUCCESS: %s -> %s "
            "(%s, row %d) "
            "EP %+d TP %+d MP %+d",
            parsed.username,
            player.lore_name,
            player.sheet_name,
            player.row_index,
            parsed.ep,
            parsed.tp,
            parsed.mp,
        )

    # ─────────────────────────────────────────────────────────
    # Final totals
    # ─────────────────────────────────────────────────────────

    result.players_updated = len(
        players_touched
    )

    # ─────────────────────────────────────────────────────────
    # Finish audit
    # ─────────────────────────────────────────────────────────

    await db.increment_dm_scans(
        dm_user.id,
        str(dm_user),
    )

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
