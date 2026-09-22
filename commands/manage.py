"""
/ranklock — prevent a player from being promoted
/unranklock — remove the lock
/add — manually add EP/TP/MP to a player
"""

from __future__ import annotations

import discord
from discord import app_commands

import config
from services import database_service as db
from services import sheets_service as sheets


STAFF_NCO_ROLES = {1475334068265685143, 1475334068265685146}


def _has_staff_permission(interaction: discord.Interaction) -> bool:
    """Staff/NCO only — for ranklock commands."""
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in interaction.user.roles}
    return bool(role_ids & STAFF_NCO_ROLES)


def _has_permission(interaction: discord.Interaction) -> bool:
    """DM, Staff, or NCO — for /add command."""
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in interaction.user.roles}
    return (
        config.DATA_MANAGER_ROLE_ID in role_ids
        or bool(role_ids & STAFF_NCO_ROLES)
    )


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    # ── /ranklock ────────────────────────────────────────────────
    @tree.command(name="ranklock", description="Block a player from promotions AND earning EP/TP/MP from scans")
    @app_commands.describe(
        username="The player's in-game username",
        reason="(Optional) Why they're locked",
        hours="(Optional) Lock duration in hours — leave empty for permanent",
    )
    async def ranklock_command(
        interaction: discord.Interaction,
        username: str,
        reason: str | None = None,
        hours: int | None = None,
    ):
        if not _has_staff_permission(interaction):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        locked = await db.ranklock_player(
            username, interaction.user.id, str(interaction.user), reason, hours
        )

        if locked:
            duration_text = f" for **{hours}** hours" if hours else " **permanently**"
            await db.log_command(
                interaction.user.id, str(interaction.user), "ranklock",
                details=f"{username}{duration_text}" + (f" — {reason}" if reason else ""),
                channel_name=interaction.channel.name,
            )
            await interaction.followup.send(
                f"🔒 **{username}** is now rank-locked{duration_text}."
                + (f"\nReason: {reason}" if reason else "")
            )
        else:
            await interaction.followup.send(f"**{username}** is already rank-locked.")

    # ── /unranklock ──────────────────────────────────────────────
    @tree.command(name="unranklock", description="Remove a player's rank lock")
    @app_commands.describe(username="The player's in-game username")
    async def unranklock_command(
        interaction: discord.Interaction,
        username: str,
    ):
        if not _has_staff_permission(interaction):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        unlocked = await db.unranklock_player(username)

        if unlocked:
            await db.log_command(
                interaction.user.id, str(interaction.user), "unranklock",
                details=username, channel_name=interaction.channel.name,
            )
            await interaction.followup.send(f"🔓 **{username}** is now unlocked.")
        else:
            await interaction.followup.send(f"**{username}** wasn't rank-locked.")

    # ── /ranklocked — view all locked players ────────────────────
    @tree.command(name="ranklocked", description="View all rank-locked players")
    async def ranklocked_command(interaction: discord.Interaction):
        if not _has_staff_permission(interaction):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        locks = await db.get_all_ranklocks()

        if not locks:
            await interaction.followup.send("No players are rank-locked.")
            return

        embed = discord.Embed(title="🔒 Rank-Locked Players", colour=discord.Colour.red())
        lines = []
        for lock in locks:
            line = f"**{lock.username}** — locked by {lock.locked_by_name}"
            if lock.reason:
                line += f" ({lock.reason})"
            if lock.expires_at:
                line += f"\n  ⏰ Expires: <t:{int(lock.expires_at.timestamp())}:R>"
            else:
                line += " — permanent"
            lines.append(line)
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed)

    # ── /add — manually add EP/TP/MP ─────────────────────────────
    @tree.command(name="add", description="Manually add or remove EP/TP/MP for a player")
    @app_commands.describe(
        username="The player's in-game username",
        stat="Which stat to change",
        amount="Amount to add (negative to subtract)",
    )
    @app_commands.choices(stat=[
        app_commands.Choice(name="EP", value="ep"),
        app_commands.Choice(name="TP", value="tp"),
        app_commands.Choice(name="MP", value="mp"),
    ])
    async def add_command(
        interaction: discord.Interaction,
        username: str,
        stat: app_commands.Choice[str],
        amount: int,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        await interaction.response.defer()

        player = await sheets.find_player(username)
        if player is None:
            await interaction.followup.send(f"❌ Player **{username}** not found.")
            return

        ep_delta = amount if stat.value == "ep" else 0
        tp_delta = amount if stat.value == "tp" else 0
        mp_delta = amount if stat.value == "mp" else 0

        try:
            await sheets.update_player_stats(
                player, ep_delta=ep_delta, tp_delta=tp_delta, mp_delta=mp_delta
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed: {exc}")
            return

        sign = "+" if amount >= 0 else ""
        old_val = {"ep": player.ep, "tp": player.tp, "mp": player.mp}[stat.value]
        new_val = old_val + amount

        await db.log_command(
            interaction.user.id, str(interaction.user), "add",
            details=f"{username} {stat.name} {sign}{amount} ({old_val} → {new_val})",
            channel_name=interaction.channel.name,
        )

        embed = discord.Embed(
            title="✅ Stats Updated",
            description=(
                f"**{player.username}** ({player.lore_name})\n"
                f"**{stat.name}**: {old_val} → **{new_val}** ({sign}{amount})"
            ),
            colour=discord.Colour.green(),
        )
        embed.set_footer(text=f"Sheet: {player.sheet_name}")
        await interaction.followup.send(embed=embed)
