"""
/deathlog command — remove a player from ALL sheets they appear in.

Usage:  /deathlog username:Smaxhy
"""

from __future__ import annotations

import discord
from discord import app_commands

import config
from services import sheets_service as sheets
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

    @tree.command(name="deathlog", description="Remove a player from all sheets (death/discharge)")
    @app_commands.describe(username="The player's in-game username or lore name")
    async def deathlog_command(
        interaction: discord.Interaction,
        username: str,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Find the player everywhere
        rows = await sheets.find_all_player_rows(username)

        if not rows:
            await interaction.followup.send(f"❌ Player **{username}** not found in any sheet.")
            return

        # Build a preview of what we're about to remove
        locations = []
        for p in rows:
            locations.append(
                f"• **{p.username}** ({p.lore_name}) — {p.sheet_name} row {p.row_index} "
                f"[{p.rank}] EP:{p.ep} TP:{p.tp} MP:{p.mp}"
            )

        # Remove them
        removed = await sheets.remove_player_rows(rows)

        # Log to database
        sheets_hit = ", ".join(set(p.sheet_name for p in rows))
        await db.log_command(
            interaction.user.id, str(interaction.user), "deathlog",
            details=f"{username} removed from {removed} location(s): {sheets_hit}",
            channel_name=interaction.channel.name,
        )

        embed = discord.Embed(
            title="💀 Player Removed (Deathlog)",
            description=(
                f"**{username}** has been wiped from **{removed}** location(s).\n\n"
                + "\n".join(locations)
            ),
            colour=discord.Colour.dark_red(),
        )
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.followup.send(embed=embed)
