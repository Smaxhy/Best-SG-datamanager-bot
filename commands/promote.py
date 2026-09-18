"""
/promote command — move a player from Ancillary to Rook or Bastion.

Usage:
    /promote username:Smaxhy detachment:Rook subdivision:Charlie
    /promote username:Smaxhy detachment:Bastion subdivision:Alpha rank:PVT
"""

from __future__ import annotations

import discord
from discord import app_commands

import config
from services import sheets_service as sheets


def _has_permission(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in interaction.user.roles}
    return config.DATA_MANAGER_ROLE_ID in role_ids or config.STAFF_ROLE_ID in role_ids


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(name="promote", description="Move a player from Ancillary to Rook or Bastion")
    @app_commands.describe(
        username="The player's in-game username",
        detachment="Destination detachment",
        subdivision="Which squadron to place them in",
        rank="Starting rank (default: RCT)",
    )
    @app_commands.choices(
        detachment=[
            app_commands.Choice(name="Rook", value="[D] ROOK"),
            app_commands.Choice(name="Bastion", value="[D] BASTION"),
        ],
        subdivision=[
            app_commands.Choice(name="Charlie (Rook left)", value="Charlie"),
            app_commands.Choice(name="Delta (Rook right)", value="Delta"),
            app_commands.Choice(name="Alpha (Bastion left)", value="Alpha"),
            app_commands.Choice(name="Bravo (Bastion right)", value="Bravo"),
        ],
        rank=[
            app_commands.Choice(name="RCT", value="RCT"),
            app_commands.Choice(name="PVT", value="PVT"),
            app_commands.Choice(name="PFC", value="PFC"),
            app_commands.Choice(name="LCPL", value="LCPL"),
        ],
    )
    async def promote_command(
        interaction: discord.Interaction,
        username: str,
        detachment: app_commands.Choice[str],
        subdivision: app_commands.Choice[str],
        rank: app_commands.Choice[str] | None = None,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        # Validate subdivision matches detachment
        valid_combos = {
            "[D] ROOK": {"Charlie", "Delta"},
            "[D] BASTION": {"Alpha", "Bravo"},
        }
        if subdivision.value not in valid_combos.get(detachment.value, set()):
            await interaction.response.send_message(
                f"❌ **{subdivision.name}** doesn't belong to **{detachment.name}**. "
                f"Valid options: {', '.join(valid_combos[detachment.value])}",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Find player in Ancillary
        player = await sheets.find_player_in_ancillary(username)
        if player is None:
            await interaction.followup.send(
                f"❌ Player **{username}** not found in Ancillary."
            )
            return

        # Promote
        try:
            result = await sheets.promote_player(
                player,
                dest_sheet=detachment.value,
                subdivision=subdivision.value,
                new_rank=rank.value if rank else "RCT",
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Promotion failed: {exc}")
            return

        embed = discord.Embed(
            title="✅ Player Promoted",
            description=result,
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Promoted by", value=interaction.user.mention)
        await interaction.followup.send(embed=embed)
