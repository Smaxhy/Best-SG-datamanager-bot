"""
/leaderboard command — view and edit Data Manager leaderboards.

Usage:
    /leaderboard                → weekly leaderboard
    /leaderboard view:alltime   → all-time leaderboard
    /leaderboard edit user:@Max amount:5    → add 5 logs to Max's current week
    /leaderboard set user:@Max amount:50    → set Max's current week to exactly 50
"""

from __future__ import annotations

import discord
from discord import app_commands

import config
from services import database_service as db


MEDALS = ["🥇", "🥈", "🥉"]


def _has_staff(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in interaction.user.roles}
    return config.STAFF_ROLE_ID in role_ids or config.NCO_ROLE_ID in role_ids


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    leaderboard_group = app_commands.Group(
        name="leaderboard",
        description="Data Manager leaderboards and stats editing",
    )

    # ── /leaderboard view ────────────────────────────────────────
    @leaderboard_group.command(name="view", description="View the leaderboard")
    @app_commands.describe(period="Weekly or all-time")
    @app_commands.choices(period=[
        app_commands.Choice(name="Weekly", value="weekly"),
        app_commands.Choice(name="All Time", value="alltime"),
    ])
    async def leaderboard_view(
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        is_alltime = period and period.value == "alltime"

        if is_alltime:
            data = await db.get_alltime_leaderboard()
            title = "🏆 All-Time Leaderboard"
        else:
            data = await db.get_weekly_leaderboard()
            year, week = db._iso_week()
            title = f"📊 Weekly Leaderboard — Week {week}, {year}"

        if not data:
            await interaction.followup.send("No data yet — run some scans first!")
            return

        embed = discord.Embed(title=title, colour=discord.Colour.gold())

        lines = []
        for i, entry in enumerate(data[:15]):
            medal = MEDALS[i] if i < 3 else f"`{i+1}.`"
            logs_label = "logs" if entry["logs"] != 1 else "log"
            scans_label = "scans" if entry["scans"] != 1 else "scan"
            lines.append(
                f"{medal} **{entry['username']}** — "
                f"{entry['logs']} {logs_label}, {entry['scans']} {scans_label}"
            )

        embed.description = "\n".join(lines)

        total_logs = sum(e["logs"] for e in data)
        total_scans = sum(e["scans"] for e in data)
        embed.set_footer(text=f"Total: {total_logs} logs across {len(data)} Data Managers")

        await interaction.followup.send(embed=embed)

    # ── /leaderboard add ─────────────────────────────────────────
    @leaderboard_group.command(name="add", description="Add or subtract logs from a DM's current week (Staff only)")
    @app_commands.describe(
        user="The Data Manager",
        amount="Number of logs to add (negative to subtract)",
    )
    async def leaderboard_add(
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
    ):
        if not _has_staff(interaction):
            await interaction.response.send_message(
                "❌ Only Staff can edit leaderboard stats.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        new_total = await db.add_dm_logs(user.id, str(user), amount)
        sign = "+" if amount >= 0 else ""
        await interaction.followup.send(
            f"✅ {sign}{amount} logs for **{user}** — new weekly total: **{new_total}**"
        )

    # ── /leaderboard set ─────────────────────────────────────────
    @leaderboard_group.command(name="set", description="Set a DM's log count for a specific week (Staff only)")
    @app_commands.describe(
        user="The Data Manager",
        amount="Exact number of logs to set",
        week="ISO week number (default: current week)",
        year="Year (default: current year)",
    )
    async def leaderboard_set(
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
        week: int | None = None,
        year: int | None = None,
    ):
        if not _has_staff(interaction):
            await interaction.response.send_message(
                "❌ Only Staff can edit leaderboard stats.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        cur_year, cur_week = db._iso_week()
        target_year = year or cur_year
        target_week = week or cur_week

        await db.set_dm_weekly_logs(user.id, str(user), target_year, target_week, max(0, amount))
        await interaction.followup.send(
            f"✅ **{user}** set to **{max(0, amount)}** logs for week {target_week}/{target_year}"
        )

    # ── /leaderboard reset ───────────────────────────────────────
    @leaderboard_group.command(name="reset", description="Reset a DM's current week to zero (Staff only)")
    @app_commands.describe(user="The Data Manager to reset")
    async def leaderboard_reset(
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        if not _has_staff(interaction):
            await interaction.response.send_message(
                "❌ Only Staff can edit leaderboard stats.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        cur_year, cur_week = db._iso_week()
        await db.set_dm_weekly_logs(user.id, str(user), cur_year, cur_week, 0)
        await interaction.followup.send(
            f"✅ **{user}** reset to **0** logs for week {cur_week}/{cur_year}"
        )

    tree.add_command(leaderboard_group)
