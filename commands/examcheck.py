"""
/examcheck command — scan a channel for exam results.

Expected message format: @member XX/14
The bot grabs the mentioned member's Discord nickname, searches the
Ancillary sheet for that name, and if the score >= 12/14:
  - checkmarks the EXAM box
  - rank-locks the player
If < 12/14 → reports them as needing to retake.

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


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(
        name="examcheck",
        description="Scan a channel for exam scores — auto-lock + checkmark passing players in Ancillary",
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

        # Collect mentioned members + their scores from message content
        # Format expected: @member XX/14  (the score is in the message text)
        results: dict[int, dict] = {}  # member_id -> {member, nickname, score}

        async for message in channel.history(after=cutoff, limit=5000):
            if not message.mentions:
                continue

            # Find all scores in the message content
            scores_in_msg = EXAM_SCORE_RE.findall(message.content)
            if not scores_in_msg:
                continue

            # If there's one mention and one score, pair them
            # If multiple mentions, try to pair each with a score in order
            mentions = message.mentions
            for i, member in enumerate(mentions):
                if member.id in results:
                    continue  # already got this person, skip dupes
                if member.bot:
                    continue

                # Grab the score — if multiple mentions, try matching by position
                score_idx = min(i, len(scores_in_msg) - 1)
                if score_idx < 0:
                    continue
                score = int(scores_in_msg[score_idx])

                # Get their server nickname (display_name = nickname if set, else username)
                nickname = member.display_name

                results[member.id] = {
                    "member": member,
                    "nickname": nickname,
                    "score": score,
                }

        # Now process everyone — split into passed/failed
        passed = {mid: r for mid, r in results.items() if r["score"] >= 12}
        failed = {mid: r for mid, r in results.items() if r["score"] < 12}

        locked_players = []
        lock_errors = []

        for mid, entry in passed.items():
            nickname = entry["nickname"]
            score = entry["score"]

            # Try to find them in Ancillary by their Discord nickname
            player = await sheets.find_ancillary_player(nickname)

            # If not found, try just the first word of the nickname
            # (nicknames might be like "John Smith | Regiment" or "John_Smith")
            if player is None and " " in nickname:
                first_word = nickname.split()[0]
                player = await sheets.find_ancillary_player(first_word)

            if player is None:
                lock_errors.append(
                    f"⚠️ **{nickname}** ({entry['member'].mention}) — "
                    f"scored {score}/14 but not found in Ancillary sheet"
                )
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
            exam_status = "+ exam ✓" if marked else "(no EXAM col found)"
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
        embed.add_field(name="Members found", value=str(len(results)), inline=True)

        if locked_players:
            text = "\n".join(locked_players[:20])
            if len(locked_players) > 20:
                text += f"\n… and {len(locked_players) - 20} more"
            embed.add_field(
                name=f"✅ Passed & Locked ({len(locked_players)})",
                value=text, inline=False,
            )

        if failed:
            fail_lines = []
            for mid, entry in list(failed.items())[:20]:
                fail_lines.append(
                    f"**{entry['nickname']}** ({entry['member'].mention}) — "
                    f"{entry['score']}/14 ❌"
                )
            text = "\n".join(fail_lines)
            if len(failed) > 20:
                text += f"\n… and {len(failed) - 20} more"
            embed.add_field(
                name=f"❌ Failed — Retake Required ({len(failed)})",
                value=text, inline=False,
            )

        if lock_errors:
            text = "\n".join(lock_errors[:10])
            if len(lock_errors) > 10:
                text += f"\n… and {len(lock_errors) - 10} more"
            embed.add_field(name="⚠️ Issues", value=text, inline=False)

        if not results:
            embed.description = (
                "No exam scores found. Make sure messages contain a @mention "
                "followed by a score like `13/14`."
            )

        embed.set_footer(text=f"By {interaction.user}")
        await interaction.followup.send(embed=embed)

        # Post retake notice for failed members
        if failed:
            retake_lines = []
            for mid, entry in list(failed.items())[:25]:
                retake_lines.append(
                    f"{entry['member'].mention} — scored **{entry['score']}/14**, "
                    f"needs to retake the exam."
                )
            retake_msg = "**Exam Retake Required:**\n" + "\n".join(retake_lines)
            await interaction.followup.send(retake_msg)
