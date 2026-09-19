"""
/examcheck command — scan a channel for exam scores in Discord nicknames.

Looks for patterns like "12/14", "13/14", "14/14" in member nicknames
mentioned in the channel within the chosen time window.

If score >= 12/14 → auto-lock the player in Ancillary + checkmark EXAM box.
If score < 12/14  → report them as needing to retake.

Usage:  /examcheck channel:#exam-results duration:24
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

import config
from services import sheets_service as sheets
from services import database_service as db

logger = logging.getLogger("bot.cmd.examcheck")

# Match exam score patterns: "12/14", "13/14", etc.
EXAM_SCORE_RE = re.compile(r"(\d{1,2})\s*/\s*14")


def _has_permission(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in interaction.user.roles}
    return (
        config.DATA_MANAGER_ROLE_ID in role_ids
        or config.STAFF_ROLE_ID in role_ids
        or config.NCO_ROLE_ID in role_ids
    )


def _extract_exam_score(text: str) -> int | None:
    """Extract the numerator from a '/14' score pattern. Returns None if no match."""
    m = EXAM_SCORE_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(
        name="examcheck",
        description="Scan a channel for exam scores in nicknames — auto-lock + checkmark passing players",
    )
    @app_commands.describe(
        channel="The Discord channel to scan for exam results",
        duration="How far back to scan (in hours)",
    )
    @app_commands.choices(duration=[
        app_commands.Choice(name="1 hour", value=1),
        app_commands.Choice(name="6 hours", value=6),
        app_commands.Choice(name="12 hours", value=12),
        app_commands.Choice(name="24 hours", value=24),
        app_commands.Choice(name="48 hours", value=48),
        app_commands.Choice(name="72 hours", value=72),
        app_commands.Choice(name="1 week", value=168),
    ])
    async def examcheck_command(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        duration: app_commands.Choice[int],
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        # Check bot can read the channel
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.view_channel or not perms.read_message_history:
            await interaction.response.send_message(
                f"❌ I don't have permission to read message history in {channel.mention}. "
                f"Check that the bot role has **View Channel** and **Read Message History** enabled.",
                ephemeral=True,
            )
            return

        # Channel restriction (if configured)
        if config.ALLOWED_CHANNEL_IDS and channel.id not in config.ALLOWED_CHANNEL_IDS:
            await interaction.response.send_message(
                f"❌ Channel {channel.mention} is not in the allowed scan list.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=duration.value)

        # Scan messages for member mentions / nicknames with exam scores
        passed: list[dict] = []     # {"member": ..., "username": ..., "score": int}
        failed: list[dict] = []
        seen_members: set[int] = set()

        async for message in channel.history(after=cutoff, limit=5000):
            # Check the message author's nickname
            if message.author.id not in seen_members:
                nick = message.author.display_name
                score = _extract_exam_score(nick)
                if score is not None:
                    seen_members.add(message.author.id)
                    entry = {
                        "member": message.author,
                        "display_name": nick,
                        "score": score,
                    }
                    if score >= 12:
                        passed.append(entry)
                    else:
                        failed.append(entry)

            # Also check mentioned members' nicknames
            for member in message.mentions:
                if member.id in seen_members:
                    continue
                nick = member.display_name
                score = _extract_exam_score(nick)
                if score is not None:
                    seen_members.add(member.id)
                    entry = {
                        "member": member,
                        "display_name": nick,
                        "score": score,
                    }
                    if score >= 12:
                        passed.append(entry)
                    else:
                        failed.append(entry)

            # Check message content for nickname-like patterns with scores
            # e.g. "PlayerName 13/14"
            content_matches = EXAM_SCORE_RE.finditer(message.content)
            for m in content_matches:
                # Already handled via author/mentions — skip pure content dupes
                pass

        # Process passing players: auto-lock + checkmark EXAM in Ancillary
        locked_players = []
        lock_errors = []

        for entry in passed:
            display = entry["display_name"]
            score = entry["score"]

            # Try to find them in Ancillary by their display name (strip score suffix)
            # Nicknames often look like "Username 13/14" — strip the score part
            clean_name = EXAM_SCORE_RE.sub("", display).strip()
            # Also try stripping common separators
            clean_name = clean_name.rstrip("-—|·").strip()

            if not clean_name:
                lock_errors.append(f"⚠️ Could not extract name from `{display}`")
                continue

            # Find player in Ancillary
            player = await sheets.find_ancillary_player(clean_name)
            if player is None:
                # Try the full display name as fallback
                player = await sheets.find_ancillary_player(display)

            if player is None:
                lock_errors.append(f"⚠️ **{clean_name}** (`{display}`) — not found in Ancillary")
                continue

            # Set EXAM checkmark
            try:
                marked = await sheets.set_exam_checkmark(player.username)
            except Exception as exc:
                lock_errors.append(f"⚠️ **{player.username}** — exam checkmark failed: {exc}")
                marked = False

            # Rank-lock the player
            try:
                was_locked = await db.ranklock_player(
                    player.username,
                    locked_by_id=interaction.user.id,
                    locked_by_name=str(interaction.user),
                    reason=f"Exam passed ({score}/14)",
                )
            except Exception as exc:
                lock_errors.append(f"⚠️ **{player.username}** — lock failed: {exc}")
                continue

            status = "✅ locked" if was_locked else "🔒 already locked"
            exam_status = "+ exam ✓" if marked else "(no EXAM col)"
            locked_players.append(
                f"**{player.username}** ({player.lore_name}) — "
                f"{score}/14 — {status} {exam_status}"
            )

        # Log the command
        await db.log_command(
            interaction.user.id, str(interaction.user), "examcheck",
            details=(
                f"#{channel.name} last {duration.value}h | "
                f"{len(passed)} passed, {len(failed)} failed, "
                f"{len(locked_players)} locked"
            ),
            channel_name=interaction.channel.name,
        )

        # Build result embed
        embed = discord.Embed(
            title="📝 Exam Check Complete",
            colour=discord.Colour.green() if not failed else discord.Colour.orange(),
        )
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Duration", value=f"Last {duration.value} hours", inline=True)
        embed.add_field(name="Members scanned", value=str(len(seen_members)), inline=True)

        if locked_players:
            # Truncate if too long for embed
            text = "\n".join(locked_players[:20])
            if len(locked_players) > 20:
                text += f"\n… and {len(locked_players) - 20} more"
            embed.add_field(name=f"✅ Passed & Locked ({len(locked_players)})", value=text, inline=False)

        if failed:
            fail_lines = []
            for entry in failed[:20]:
                clean = EXAM_SCORE_RE.sub("", entry["display_name"]).strip().rstrip("-—|·").strip()
                fail_lines.append(f"**{clean or entry['display_name']}** — {entry['score']}/14 ❌")
            text = "\n".join(fail_lines)
            if len(failed) > 20:
                text += f"\n… and {len(failed) - 20} more"
            embed.add_field(name=f"❌ Failed — Retake Required ({len(failed)})", value=text, inline=False)

        if lock_errors:
            text = "\n".join(lock_errors[:10])
            if len(lock_errors) > 10:
                text += f"\n… and {len(lock_errors) - 10} more"
            embed.add_field(name="⚠️ Issues", value=text, inline=False)

        if not passed and not failed:
            embed.description = "No exam scores found in nicknames during this period."

        embed.set_footer(text=f"By {interaction.user}")
        await interaction.followup.send(embed=embed)

        # Send DMs or channel messages to failed members telling them to retake
        if failed:
            retake_lines = []
            for entry in failed:
                member = entry["member"]
                retake_lines.append(f"{member.mention} — scored **{entry['score']}/14**, needs to retake the exam.")
            retake_msg = "**Exam Retake Required:**\n" + "\n".join(retake_lines[:25])
            await interaction.followup.send(retake_msg)
