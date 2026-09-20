"""
/purge command — bulk delete messages in a channel (max 20).

Usage:  /purge amount:10
"""

from __future__ import annotations

import discord
from discord import app_commands

import config
from services import database_service as db


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

    @tree.command(name="purge", description="Delete up to 20 messages in the current channel")
    @app_commands.describe(amount="Number of messages to delete (1-20)")
    async def purge_command(
        interaction: discord.Interaction,
        amount: int,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        if amount < 1 or amount > 20:
            await interaction.response.send_message(
                "❌ Amount must be between 1 and 20.",
                ephemeral=True,
            )
            return

        # Check bot has manage_messages permission
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if not perms.manage_messages:
            await interaction.response.send_message(
                "❌ I don't have **Manage Messages** permission in this channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Only delete messages from the last 3 days
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)

        deleted = await interaction.channel.purge(
            limit=amount,
            after=cutoff,
        )

        await db.log_command(
            interaction.user.id, str(interaction.user), "purge",
            details=f"Deleted {len(deleted)} messages in #{interaction.channel.name}",
            channel_name=interaction.channel.name,
        )

        await interaction.followup.send(
            f"🗑️ Deleted **{len(deleted)}** messages.",
            ephemeral=True,
        )
