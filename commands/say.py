"""
/say command — make the bot post a message in any channel.

Usage:
    /say message:Hello everyone!
    /say message:Meeting at 5pm channel:#announcements
"""

from __future__ import annotations

import discord
from discord import app_commands

import config


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

    @tree.command(name="say", description="Make the bot send a message")
    @app_commands.describe(
        message="What the bot should say",
        channel="(Optional) Channel to send in — defaults to current channel",
    )
    async def say_command(
        interaction: discord.Interaction,
        message: str,
        channel: discord.TextChannel | None = None,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel

        try:
            await target.send(message)
            if channel and channel.id != interaction.channel_id:
                await interaction.response.send_message(
                    f"✅ Message sent in {channel.mention}", ephemeral=True
                )
            else:
                await interaction.response.send_message("✅ Sent", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(
                f"❌ Couldn't send message: {exc}", ephemeral=True
            )
