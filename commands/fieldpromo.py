"""
/fieldpromo — promote a player without checking the point requirements.

Usage:
    /fieldpromo username:Smaxhy rank:PFC
        → field-promotes Smaxhy to PFC where they are; points are kept as-is

    /fieldpromo username:Smaxhy rank:PVT detachment:Rook subdivision:Charlie
        → moves them from Ancillary into Rook Charlie as PVT, keeping all points

Staff and NCO only. Rank locks still apply. Every use is logged and announced.
"""

from __future__ import annotations

import discord
from discord import app_commands

import config
from commands.promote import PROMOTION_CHANNEL_ID, _blocked_by_ranklock
from services import database_service as db
from services import sheets_service as sheets

VALID_SUBDIVISIONS = {
    "[D] ROOK": {"Charlie", "Delta"},
    "[D] BASTION": {"Alpha", "Bravo"},
}


def _has_permission(interaction: discord.Interaction) -> bool:
    """Staff or NCO — field promotions skip the rules, so Data Managers alone can't."""
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
    return config.STAFF_ROLE_ID in role_ids or config.NCO_ROLE_ID in role_ids


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(name="fieldpromo", description="Promote a player without the point requirements (Staff/NCO)")
    @app_commands.describe(
        username="The player's in-game username",
        rank="New rank",
        detachment="(Optional) Move them into this detachment",
        subdivision="(With detachment) Which squadron",
        reason="(Optional) Why — shown in the log and the announcement",
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
    async def fieldpromo_command(
        interaction: discord.Interaction,
        username: str,
        rank: app_commands.Choice[str],
        detachment: app_commands.Choice[str] | None = None,
        subdivision: app_commands.Choice[str] | None = None,
        reason: str | None = None,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ Only Staff or NCOs can field-promote.", ephemeral=True
            )
            return
        if bool(detachment) != bool(subdivision):
            await interaction.response.send_message(
                "❌ Pick both a detachment and a subdivision, or neither.", ephemeral=True
            )
            return
        if detachment and subdivision.value not in VALID_SUBDIVISIONS.get(detachment.value, set()):
            await interaction.response.send_message(
                f"❌ **{subdivision.name}** doesn't belong to **{detachment.name}**.", ephemeral=True
            )
            return

        await interaction.response.defer()

        player = await sheets.find_player(username)
        if player is None:
            await interaction.followup.send(f"❌ Player **{username}** not found.")
            return
        if await _blocked_by_ranklock(interaction, username, player):
            return

        old_rank = (player.rank or "").strip().upper() or "RCT"
        new_rank = rank.value
        if not detachment and old_rank == new_rank:
            await interaction.followup.send(f"ℹ️ **{player.username}** is already **{new_rank}**.")
            return

        try:
            if detachment:
                result = await sheets.transfer_player(
                    player,
                    dest_sheet=detachment.value,
                    subdivision=subdivision.value,
                    new_rank=new_rank,
                    force=True,
                )
                points = (result["new_ep"], result["new_tp"], result["new_mp"])
                where = f"**{player.sheet_name}** → **{detachment.name} / {subdivision.name}**"
            else:
                await sheets.promote_rank(player, new_rank, force=True)
                points = (player.ep, player.tp, player.mp)
                where = f"**{player.sheet_name}**"
        except Exception as exc:
            await interaction.followup.send(f"❌ Field promotion failed: {exc}")
            return

        ep, tp, mp = points
        await db.log_command(
            interaction.user.id, str(interaction.user), "fieldpromo",
            details=(
                f"{player.username} {old_rank} → {new_rank} in {player.sheet_name}"
                + (f" → {detachment.name}/{subdivision.name}" if detachment else "")
                + f" | EP {ep} TP {tp} MP {mp} kept"
                + (f" | reason: {reason}" if reason else "")
            ),
            channel_name=getattr(interaction.channel, "name", None),
        )

        embed = discord.Embed(
            title="🎖️ Field Promotion",
            description=(
                f"**{player.username}** ({player.lore_name}) **{old_rank}** → **{new_rank}**\n"
                f"Sheet: {where}\n"
                f"Requirements skipped — points kept: EP {ep} · TP {tp} · MP {mp}"
            ),
            colour=discord.Colour.gold(),
        )
        if reason:
            embed.add_field(name="Reason", value=reason[:1024], inline=False)
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.followup.send(embed=embed)

        await db.increment_dm_score(interaction.user.id, str(interaction.user), logs=1)

        try:
            announce_ch = bot.get_channel(PROMOTION_CHANNEL_ID)
            if announce_ch:
                await announce_ch.send(
                    f"🎖️ **{player.username}** ({player.lore_name}) field promoted "
                    f"**{old_rank}** → **{new_rank}**"
                    + (f" into **{detachment.name}**" if detachment else "")
                    + (f" — {reason}" if reason else ""),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:
            pass
