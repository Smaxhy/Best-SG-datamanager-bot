"""
/logs command — view command usage in the last 24 hours.

Usage:
    /logs
    /logs hours:48
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

    @tree.command(name="logs", description="View command usage history")
    @app_commands.describe(hours="How far back to look (default: 24 hours)")
    async def logs_command(
        interaction: discord.Interaction,
        hours: int = 24,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        entries = await db.get_recent_commands(hours)

        if not entries:
            await interaction.followup.send(f"No commands used in the last {hours} hours.")
            return

        embed = discord.Embed(
            title=f"📜 Command Log — Last {hours} hours",
            colour=discord.Colour.purple(),
        )

        lines = []
        for entry in entries[:25]:
            time_str = entry.executed_at.strftime("%m/%d %H:%M")
            detail = f" — {entry.details}" if entry.details else ""
            lines.append(
                f"`{time_str}` **{entry.discord_username}** "
                f"used `/{entry.command}`{detail}"
            )

        embed.description = "\n".join(lines)

        if len(entries) > 25:
            embed.set_footer(text=f"Showing 25 of {len(entries)} entries")
        else:
            embed.set_footer(text=f"{len(entries)} entries total")

        await interaction.followup.send(embed=embed)
