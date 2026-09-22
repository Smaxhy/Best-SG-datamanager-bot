"""
/trainingban, /trainingunban, /trainingbanned commands.

Training ban:
  - Hides training channel from the player
  - Posts a notice in the training ban log channel
  - Stores ban in DB with duration and reason
  - Auto-expires if duration is set

Channel IDs:
  - Training channel (hidden from banned players): 1475334069259735112
  - Training ban log channel (notice posted here):  1475334069259735113
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

import config
from services import database_service as db

logger = logging.getLogger("bot.cmd.trainingban")

TRAINING_CHANNEL_ID = 1475334069259735112
TRAINING_BAN_LOG_CHANNEL_ID = 1475334069259735113


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

    # ── /trainingban ────────────────────────────────────────────────

    @tree.command(
        name="trainingban",
        description="Ban a player from the training channel",
    )
    @app_commands.describe(
        member="The Discord member to training ban",
        reason="Reason for the training ban",
        duration="How long the ban lasts",
    )
    @app_commands.choices(duration=[
        app_commands.Choice(name="1 hour", value=1),
        app_commands.Choice(name="6 hours", value=6),
        app_commands.Choice(name="12 hours", value=12),
        app_commands.Choice(name="24 hours", value=24),
        app_commands.Choice(name="48 hours", value=48),
        app_commands.Choice(name="72 hours", value=72),
        app_commands.Choice(name="1 week", value=168),
        app_commands.Choice(name="2 weeks", value=336),
        app_commands.Choice(name="1 month", value=720),
        app_commands.Choice(name="Permanent", value=0),
    ])
    async def trainingban_command(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
        duration: app_commands.Choice[int],
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False)

        hours = duration.value if duration.value > 0 else None
        username = member.display_name

        # Store in database
        was_banned = await db.trainingban_player(
            username=username,
            banned_by_id=interaction.user.id,
            banned_by_name=str(interaction.user),
            discord_id=member.id,
            reason=reason,
            hours=hours,
        )

        if not was_banned:
            await interaction.followup.send(
                f"⚠️ **{username}** is already training banned."
            )
            return

        # Hide training channel from this member
        training_channel = bot.get_channel(TRAINING_CHANNEL_ID)
        if training_channel:
            try:
                await training_channel.set_permissions(
                    member,
                    view_channel=False,
                    reason=f"Training ban: {reason}",
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ I don't have permission to modify channel permissions."
                )
                return

        # Calculate expiry
        if hours:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
            expires_str = f"<t:{int(expires_at.timestamp())}:F>"
            duration_str = duration.name
        else:
            expires_str = "Never (Permanent)"
            duration_str = "Permanent"

        # Log command
        await db.log_command(
            interaction.user.id, str(interaction.user), "trainingban",
            details=f"{username} banned for {duration_str} — {reason}",
            channel_name=interaction.channel.name,
        )

        # Send confirmation in current channel
        embed = discord.Embed(
            title="🚫 Training Ban Issued",
            colour=discord.Colour.red(),
        )
        embed.add_field(name="Player", value=f"{member.mention} ({username})", inline=False)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(name="Expires", value=expires_str, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="By", value=interaction.user.mention, inline=True)
        await interaction.followup.send(embed=embed)

        # Post notice in the training ban log channel
        log_channel = bot.get_channel(TRAINING_BAN_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title="🚫 Training Ban",
                colour=discord.Colour.red(),
            )
            log_embed.add_field(name="Player", value=f"{member.mention} ({username})", inline=False)
            log_embed.add_field(name="Duration", value=duration_str, inline=True)
            log_embed.add_field(name="Expires", value=expires_str, inline=True)
            log_embed.add_field(name="Reason", value=reason, inline=False)
            log_embed.add_field(name="Banned by", value=interaction.user.mention, inline=True)
            log_embed.set_footer(text=f"Issued {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
            try:
                await log_channel.send(embed=log_embed)
            except Exception:
                logger.exception("Failed to post training ban notice")

    # ── /trainingunban ──────────────────────────────────────────────

    @tree.command(
        name="trainingunban",
        description="Remove a training ban from a player",
    )
    @app_commands.describe(member="The Discord member to unban from training")
    async def trainingunban_command(
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False)

        username = member.display_name
        was_unbanned = await db.trainingunban_player(username)

        if not was_unbanned:
            await interaction.followup.send(f"⚠️ **{username}** is not training banned.")
            return

        # Restore training channel access
        training_channel = bot.get_channel(TRAINING_CHANNEL_ID)
        if training_channel:
            try:
                await training_channel.set_permissions(
                    member,
                    overwrite=None,
                    reason="Training ban lifted",
                )
            except discord.Forbidden:
                pass

        await db.log_command(
            interaction.user.id, str(interaction.user), "trainingunban",
            details=f"{username} unbanned from training",
            channel_name=interaction.channel.name,
        )

        await interaction.followup.send(
            f"✅ **{username}** ({member.mention}) has been unbanned from training."
        )

        # Post notice in log channel
        log_channel = bot.get_channel(TRAINING_BAN_LOG_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(
                    f"✅ **{username}** ({member.mention}) training ban lifted by {interaction.user.mention}"
                )
            except Exception:
                pass

    # ── /trainingbanned ─────────────────────────────────────────────

    @tree.command(
        name="trainingbanned",
        description="View all active training bans",
    )
    async def trainingbanned_command(interaction: discord.Interaction):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        bans = await db.get_all_trainingbans()

        if not bans:
            await interaction.followup.send("✅ No active training bans.", ephemeral=True)
            return

        lines = []
        for ban in bans:
            if ban.expires_at:
                expires_str = f"<t:{int(ban.expires_at.timestamp())}:R>"
            else:
                expires_str = "Permanent"

            reason_str = ban.reason or "No reason"
            discord_mention = f"<@{ban.discord_id}>" if ban.discord_id else ban.username

            lines.append(
                f"**{ban.username}** ({discord_mention}) — "
                f"expires {expires_str} — {reason_str}"
            )

        embed = discord.Embed(
            title=f"🚫 Active Training Bans ({len(bans)})",
            description="\n".join(lines[:25]),
            colour=discord.Colour.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
