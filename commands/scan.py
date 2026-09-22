"""
/scan command — triggers a channel scan for player activity logs.

Usage:  /scan channel:#rook-logs
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

import config
from services.discord_service import run_scan
from services import database_service as db

logger = logging.getLogger("bot.cmd.scan")


def has_required_role(interaction: discord.Interaction) -> bool:
    """Check the invoker has Data Manager or Staff role."""
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in interaction.user.roles}
    return (
        config.DATA_MANAGER_ROLE_ID in role_ids
        or config.STAFF_ROLE_ID in role_ids
        or config.NCO_ROLE_ID in role_ids
    )


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:
    """Register the /scan command on the provided tree."""

    @tree.command(name="scan", description="Scan a channel for player activity logs")
    @app_commands.describe(
        channel="The Discord channel to scan",
        exclude="Usernames to skip this scan (comma-separated, e.g. Smaxhy,foot3331)",
    )
    async def scan_command(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        exclude: str | None = None,
    ):
        # ── Permission check ─────────────────────────────────────
        if not has_required_role(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        # ── Channel restriction (optional) ───────────────────────
        if config.ALLOWED_CHANNEL_IDS and channel.id not in config.ALLOWED_CHANNEL_IDS:
            await interaction.response.send_message(
                f"❌ Channel {channel.mention} is not in the allowed scan list.",
                ephemeral=True,
            )
            return

        # ── Bot must be able to read the channel ─────────────────
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.read_message_history:
            await interaction.response.send_message(
                f"❌ I don't have permission to read message history in {channel.mention}.",
                ephemeral=True,
            )
            return

        # ── Acknowledge (we may take a while) ────────────────────
        await interaction.response.defer(ephemeral=False)

        await db.log_command(
            interaction.user.id, str(interaction.user), "scan",
            details=f"#{channel.name}", channel_name=interaction.channel.name,
        )

        # Parse excluded usernames
        excluded_names: set[str] = set()
        if exclude:
            excluded_names = {name.strip().lower() for name in exclude.split(",") if name.strip()}

        try:
            result = await run_scan(channel, interaction.user, excluded_names=excluded_names)
        except Exception:
            logger.exception("Scan failed.")
            await interaction.followup.send("💥 Scan failed due to an internal error. Check the logs.")
            return

        # ── Fetch DM's current weekly score ──────────────────────
        from services.database_service import get_dm_current_week
        dm_week = await get_dm_current_week(interaction.user.id)
        weekly_total = dm_week.logs_processed if dm_week else 0

        # ── Build summary embed ──────────────────────────────────
        embed = discord.Embed(
            title="📊 Scan Complete",
            colour=discord.Colour.green() if result.errors == 0 else discord.Colour.orange(),
        )
        embed.add_field(name="Channel", value=f"#{result.channel_name}", inline=True)
        embed.add_field(name="Time range", value=f"Last {config.SCAN_HOURS} hours", inline=True)
        embed.add_field(name="Data Manager", value=interaction.user.mention, inline=True)
        embed.add_field(name="Messages checked", value=str(result.messages_checked), inline=True)
        embed.add_field(name="Valid logs found", value=str(result.valid_logs), inline=True)
        embed.add_field(name="New logs processed", value=str(result.new_processed), inline=True)
        embed.add_field(name="Already processed", value=str(result.already_processed), inline=True)
        embed.add_field(name="Players updated", value=str(result.players_updated), inline=True)
        embed.add_field(name="Players not found", value=str(result.players_not_found), inline=True)
        embed.add_field(name="Invalid logs", value=str(result.invalid_logs), inline=True)
        embed.add_field(name="Lore mismatches", value=str(result.lore_mismatches), inline=True)
        embed.add_field(name="Errors", value=str(result.errors), inline=True)
        embed.add_field(name="EP added", value=f"+{result.ep_added}", inline=True)
        embed.add_field(name="TP added", value=f"+{result.tp_added}", inline=True)
        embed.add_field(name="MP added", value=f"+{result.mp_added}", inline=True)
        embed.add_field(
            name="Your weekly score",
            value=f"**{weekly_total}** logs this week",
            inline=False,
        )

        await interaction.followup.send(embed=embed)

        # ── Flagged items (sent separately so the embed stays clean)
        if result.flagged_messages:
            flag_text = "\n".join(result.flagged_messages[:20])
            if len(result.flagged_messages) > 20:
                flag_text += f"\n… and {len(result.flagged_messages) - 20} more"
            await interaction.followup.send(f"**Flagged items:**\n{flag_text}")
