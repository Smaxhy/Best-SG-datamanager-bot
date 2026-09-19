"""
/promote command — rank promotions and detachment transfers.

Usage:
    /promote username:Smaxhy rank:PFC
        → promotes Smaxhy to PFC in their current sheet, reorders by rank

    /promote username:Smaxhy detachment:Rook subdivision:Charlie rank:RCT
        → transfers from Ancillary to Rook Charlie as RCT
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


PROMOTION_CHANNEL_ID = 1511781917693972601


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(name="promote", description="Promote a player's rank or transfer them between detachments")
    @app_commands.describe(
        username="The player's in-game username",
        rank="New rank",
        detachment="(Transfer only) Destination detachment",
        subdivision="(Transfer only) Which squadron",
    )
    @app_commands.choices(
        rank=[
            app_commands.Choice(name="RCT", value="RCT"),
            app_commands.Choice(name="PVT", value="PVT"),
            app_commands.Choice(name="PFC", value="PFC"),
            app_commands.Choice(name="LCPL", value="LCPL"),
        ],
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
    )
    async def promote_command(
        interaction: discord.Interaction,
        username: str,
        rank: app_commands.Choice[str],
        detachment: app_commands.Choice[str] | None = None,
        subdivision: app_commands.Choice[str] | None = None,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        # If transferring, both detachment and subdivision are required
        if detachment and not subdivision:
            await interaction.response.send_message(
                "❌ You need to pick a subdivision when transferring.",
                ephemeral=True,
            )
            return
        if subdivision and not detachment:
            await interaction.response.send_message(
                "❌ You need to pick a detachment when transferring.",
                ephemeral=True,
            )
            return

        # Validate subdivision matches detachment
        if detachment and subdivision:
            valid_combos = {
                "[D] ROOK": {"Charlie", "Delta"},
                "[D] BASTION": {"Alpha", "Bravo"},
            }
            if subdivision.value not in valid_combos.get(detachment.value, set()):
                await interaction.response.send_message(
                    f"❌ **{subdivision.name}** doesn't belong to **{detachment.name}**.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        # Check ranklock
        lock = await db.is_ranklocked(username)
        if lock:
            await interaction.followup.send(
                f"🔒 **{username}** is rank-locked by **{lock.locked_by_name}**"
                + (f" — {lock.reason}" if lock.reason else "")
                + "\nUse `/unranklock` to unlock them first."
            )
            return

        if detachment and subdivision:
            await db.log_command(
                interaction.user.id, str(interaction.user), "promote",
                details=f"{username} → {detachment.name}/{subdivision.name} as {rank.value}",
                channel_name=interaction.channel.name,
            )
            # ── TRANSFER: move player to new detachment ──────────
            player = await sheets.find_player(username)
            if player is None:
                await interaction.followup.send(f"❌ Player **{username}** not found.")
                return

            try:
                result = await sheets.transfer_player(
                    player,
                    dest_sheet=detachment.value,
                    subdivision=subdivision.value,
                    new_rank=rank.value,
                )
            except Exception as exc:
                await interaction.followup.send(f"❌ Transfer failed: {exc}")
                return

            embed = discord.Embed(
                title="🔄 Player Transferred",
                description=result,
                colour=discord.Colour.blue(),
            )
            embed.add_field(name="By", value=interaction.user.mention)
            await interaction.followup.send(embed=embed)

            # Log transfer as weekly DM score
            await db.increment_dm_score(interaction.user.id, str(interaction.user), logs=1)
            await db.increment_dm_scans(interaction.user.id, str(interaction.user))

            # Announce in promotions channel
            try:
                announce_ch = bot.get_channel(PROMOTION_CHANNEL_ID)
                if announce_ch:
                    await announce_ch.send(
                        f"📋 **{player.username}** ({player.lore_name}) transferred to "
                        f"**{detachment.name}** → **{subdivision.name}** as **{rank.value}**"
                    )
            except Exception:
                pass

        else:
            # ── RANK PROMOTION: change rank in current sheet ─────
            await db.log_command(
                interaction.user.id, str(interaction.user), "promote",
                details=f"{username} → {rank.value}",
                channel_name=interaction.channel.name,
            )
            player = await sheets.find_player(username)
            if player is None:
                await interaction.followup.send(f"❌ Player **{username}** not found.")
                return

            old_rank = player.rank
            try:
                result_msg = await sheets.promote_rank(player, rank.value)
            except ValueError as exc:
                # Requirements not met
                await interaction.followup.send(str(exc))
                return
            except Exception as exc:
                await interaction.followup.send(f"❌ Promotion failed: {exc}")
                return

            embed = discord.Embed(
                title="⬆️ Player Promoted",
                description=result_msg,
                colour=discord.Colour.green(),
            )
            embed.set_footer(text=f"Sheet: {player.sheet_name}")
            embed.add_field(name="By", value=interaction.user.mention)
            await interaction.followup.send(embed=embed)

            # Log promotion as weekly DM score
            await db.increment_dm_score(interaction.user.id, str(interaction.user), logs=1)
            await db.increment_dm_scans(interaction.user.id, str(interaction.user))

            # Announce in promotions channel
            try:
                announce_ch = bot.get_channel(PROMOTION_CHANNEL_ID)
                if announce_ch:
                    await announce_ch.send(
                        f"⬆️ **{player.username}** ({player.lore_name}) promoted "
                        f"**{old_rank}** → **{rank.value}**"
                    )
            except Exception:
                pass
