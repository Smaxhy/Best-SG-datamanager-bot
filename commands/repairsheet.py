"""
/repairsheet — turn text "TRUE"/"FALSE" back into real checkboxes and text
numbers in EP/TP/MP back into numbers, on every player tab.

Older versions of the bot wrote these as text when re-sorting a section, which
shows as "TRUE" with a red corner instead of a ticked box. Safe to run any time:
only cells that are actually text get rewritten, and ticks are kept as they were.
"""

from __future__ import annotations

import discord
from discord import app_commands

from commands.scan import has_required_role
from services import database_service as db
from services import sheets_service as sheets


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(name="repairsheet", description="Fix checkboxes/numbers the bot turned into text on the sheet")
    async def repairsheet_command(interaction: discord.Interaction):
        if not has_required_role(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.defer()
        fixed = await sheets.repair_sheet_types()

        lines = []
        for tab, count in fixed.items():
            if count < 0:
                lines.append(f"⚠️ **{tab}** — couldn't be read or written, check the logs")
            elif count:
                lines.append(f"✅ **{tab}** — {count} cell{'s' if count != 1 else ''} fixed")
        total = sum(c for c in fixed.values() if c > 0)
        if not lines:
            lines.append("✅ Nothing to fix — every checkbox and number is already the right type.")

        await db.log_command(
            interaction.user.id, str(interaction.user), "repairsheet",
            details=f"{total} cells fixed", channel_name=getattr(interaction.channel, "name", None),
        )
        await interaction.followup.send("**🧰 Sheet repair**\n" + "\n".join(lines))
