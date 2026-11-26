"""
/repairsheet — turn text "TRUE"/"FALSE" back into real checkboxes and text
numbers in EP/TP/MP back into numbers, on every player tab.

/repairsheet formatting:True — also restyle player rows that look different
from the rest of their table (e.g. plain Arial rows at the bottom) to match
the style most of that table's players have.

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

    @tree.command(name="repairsheet", description="Fix checkboxes/numbers turned into text; optionally fix row styling")
    @app_commands.describe(formatting="Also restyle rows that look different from the rest of their table")
    async def repairsheet_command(interaction: discord.Interaction, formatting: bool = False):
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
            lines.append("✅ Checkboxes and numbers are all the right type.")

        if formatting:
            restyled = await sheets.repair_row_formatting()
            any_rows = False
            for tab, rows in restyled.items():
                if rows:
                    any_rows = True
                    shown = ", ".join(str(r) for r in rows[:25]) + (" …" if len(rows) > 25 else "")
                    lines.append(f"🎨 **{tab}** — restyled row{'s' if len(rows) != 1 else ''} {shown}")
            if not any_rows:
                lines.append("🎨 Every player row already matches its table's style.")

        await db.log_command(
            interaction.user.id, str(interaction.user), "repairsheet",
            details=f"{total} cells fixed", channel_name=getattr(interaction.channel, "name", None),
        )
        await interaction.followup.send("**🧰 Sheet repair**\n" + "\n".join(lines))
