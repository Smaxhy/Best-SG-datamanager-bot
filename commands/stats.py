"""
/dmstats command — view Data Manager statistics.

Usage:
    /dmstats              → your own stats
    /dmstats user:@Max    → someone else's stats (Staff only)
"""

from __future__ import annotations

import discord
from discord import app_commands

import config
from services.stats_service import current_week_summary, full_history


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(name="dmstats", description="View Data Manager statistics")
    @app_commands.describe(user="(Optional) Another user to look up — Staff only")
    async def dmstats_command(
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ):
        target = user or interaction.user

        # If looking up someone else, require Staff role
        if user and user.id != interaction.user.id:
            role_ids = {r.id for r in interaction.user.roles}
            if config.STAFF_ROLE_ID not in role_ids:
                await interaction.response.send_message(
                    "❌ Only Staff can view another user's stats.", ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True)

        current = await current_week_summary(target.id)
        history = await full_history(target.id)

        embed = discord.Embed(
            title=f"📈 Data Manager Statistics",
            colour=discord.Colour.blue(),
        )
        embed.set_author(name=str(target), icon_url=target.display_avatar.url)
        embed.add_field(
            name="Current week",
            value=(
                f"Week {current['week']} of {current['year']}\n"
                f"Logs processed: **{current['logs']}**\n"
                f"Scans completed: **{current['scans']}**"
            )
            if current["week"]
            else "No activity this week.",
            inline=False,
        )

        if history:
            lines = []
            for h in history[:10]:
                lines.append(f"Wk {h['week']}/{h['year']} — {h['logs']} logs, {h['scans']} scans")
            embed.add_field(name="History (last 10 weeks)", value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed)
