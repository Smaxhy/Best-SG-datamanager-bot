"""
/addlog command — add a new player to a sheet.

Usage:
    /addlog username:Smaxhy lorename:Jessica Cruz sheet:Ancillary
    /addlog username:Smaxhy lorename:Jessica Cruz sheet:Rook subdivision:Charlie
    /addlog username:Smaxhy lorename:Jessica Cruz sheet:Device Wielders device_status:Active sourced_by:NCO
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

    @tree.command(name="addlog", description="Add a new player to a sheet")
    @app_commands.describe(
        username="The player's in-game username",
        lorename="The player's lore name (first and last)",
        sheet="Which sheet to add them to",
        subdivision="(Rook/Bastion/Valkyrie only) Which subdivision",
        device_status="(Device Wielders only) Device status",
        sourced_by="(Device Wielders only) Who sourced the device",
    )
    @app_commands.choices(sheet=[
        app_commands.Choice(name="Ancillary", value="[C] ANCILLARY"),
        app_commands.Choice(name="Rook", value="[D] ROOK"),
        app_commands.Choice(name="Bastion", value="[D] BASTION"),
        app_commands.Choice(name="Valkyrie", value="[U] VALKYRIE"),
        app_commands.Choice(name="Medical", value="[U] MEDICAL"),
        app_commands.Choice(name="Research", value="[U] RESEARCH"),
        app_commands.Choice(name="Cannoneer", value="[U] CANNONEER"),
        app_commands.Choice(name="Peacekeeper", value="[S] PEACEKEEPER"),
        app_commands.Choice(name="Device Wielders", value="DEVICE WIELDERS"),
    ])
    @app_commands.choices(subdivision=[
        app_commands.Choice(name="Charlie (Rook left)", value="Charlie"),
        app_commands.Choice(name="Delta (Rook right)", value="Delta"),
        app_commands.Choice(name="Alpha (Bastion left)", value="Alpha"),
        app_commands.Choice(name="Bravo (Bastion right)", value="Bravo"),
        app_commands.Choice(name="Aether (Valkyrie)", value="Aether"),
        app_commands.Choice(name="Helios (Valkyrie)", value="Helios"),
    ])
    @app_commands.choices(device_status=[
        app_commands.Choice(name="Active", value="Active"),
        app_commands.Choice(name="Inactive", value="Inactive"),
        app_commands.Choice(name="Pending", value="Pending"),
        app_commands.Choice(name="Revoked", value="Revoked"),
    ])
    async def addlog_command(
        interaction: discord.Interaction,
        username: str,
        lorename: str,
        sheet: app_commands.Choice[str],
        subdivision: app_commands.Choice[str] | None = None,
        device_status: app_commands.Choice[str] | None = None,
        sourced_by: str | None = None,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        is_device = sheet.value == "DEVICE WIELDERS"

        # Rook/Bastion/Valkyrie require a subdivision
        needs_sub = sheet.value in ("[D] ROOK", "[D] BASTION", "[U] VALKYRIE")
        if needs_sub and subdivision is None:
            await interaction.response.send_message(
                f"❌ **{sheet.name}** requires a subdivision.",
                ephemeral=True,
            )
            return

        # Validate subdivision matches sheet
        if subdivision:
            rook_subs = ("Charlie", "Delta")
            bastion_subs = ("Alpha", "Bravo")
            valkyrie_subs = ("Aether", "Helios")
            if sheet.value == "[D] ROOK" and subdivision.value not in rook_subs:
                await interaction.response.send_message(
                    f"❌ Rook only has Charlie or Delta, not {subdivision.name}.",
                    ephemeral=True,
                )
                return
            if sheet.value == "[D] BASTION" and subdivision.value not in bastion_subs:
                await interaction.response.send_message(
                    f"❌ Bastion only has Alpha or Bravo, not {subdivision.name}.",
                    ephemeral=True,
                )
                return
            if sheet.value == "[U] VALKYRIE" and subdivision.value not in valkyrie_subs:
                await interaction.response.send_message(
                    f"❌ Valkyrie only has Aether or Helios, not {subdivision.name}.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        # Duplicate check — only block if they already exist in the SAME sheet
        # (a player CAN be in Bastion AND Device Wielders simultaneously)
        existing = await sheets.find_player(username)
        if existing and existing.sheet_name.strip().upper() == sheet.value.strip().upper():
            await interaction.followup.send(
                f"⚠️ **{username}** already exists in **{existing.sheet_name}** "
                f"(row {existing.row_index})."
            )
            return

        # Build extra fields for Device Wielders
        extra_fields = {}
        if is_device:
            if device_status:
                extra_fields["DEVICE STATUS"] = device_status.value
            if sourced_by:
                extra_fields["SOURCED BY"] = sourced_by

        try:
            result_msg = await sheets.add_player_to_sheet(
                username=username,
                lore_name=lorename,
                sheet_name=sheet.value,
                subdivision=subdivision.value if subdivision else None,
                extra_fields=extra_fields,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to add player: {exc}")
            return

        await db.log_command(
            interaction.user.id, str(interaction.user), "addlog",
            details=f"Added {username} ({lorename}) to {sheet.name}"
                    + (f" → {subdivision.name}" if subdivision else ""),
            channel_name=interaction.channel.name,
        )

        embed = discord.Embed(
            title="✅ Player Added",
            description=result_msg,
            colour=discord.Colour.green(),
        )
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.followup.send(embed=embed)
