"""
/sheetcheck — show exactly how the bot reads the spreadsheet: which tabs it
searches, which tables and columns it finds on each, how many players, which
configured tabs are missing, and which usernames appear more than once.
Read-only. Use it when players show as "not found" or land in the wrong place.
"""

from __future__ import annotations

import discord
from discord import app_commands

from commands.scan import _send, chunk_lines, has_required_role
from services import sheets_service as sheets


def format_report(report: dict) -> list[str]:
    lines: list[str] = []
    for tab in report["tabs"]:
        if not tab["searched"]:
            lines.append(f"⚪ **{tab['title']}** — not searched (not in PLAYER_SHEETS)")
            continue
        if tab["error"]:
            lines.append(f"❌ **{tab['title']}** — couldn't read: {tab['error']}")
            continue
        if not tab["tables"]:
            lines.append(f"⚠️ **{tab['title']}** — no table with a USERNAME header found")
            continue
        total = sum(t["players"] for t in tab["tables"])
        lines.append(f"✅ **{tab['title']}** — {len(tab['tables'])} table(s), {total} players")
        for t in tab["tables"]:
            cols = ", ".join(f"{k} {c}" for k, c in t["columns"].items())
            missing = [k for k in ("LORE NAME", "EP", "TP", "MP") if k not in t["columns"]]
            note = f" — ⚠️ no {', '.join(missing)} column" if missing else ""
            lines.append(f"  • table at {t['header_cell']}: {t['players']} players · {cols}{note}")

    if report["missing"]:
        lines.append("")
        lines.append("⚠️ In PLAYER_SHEETS but no such tab: " + ", ".join(f"**{m}**" for m in report["missing"]))

    lines.append("")
    if report["duplicates"]:
        lines.append(f"👥 **Usernames in more than one row ({len(report['duplicates'])}):**")
        for spots in list(report["duplicates"].values())[:30]:
            lines.append("  • " + " · ".join(spots))
        if len(report["duplicates"]) > 30:
            lines.append(f"  … and {len(report['duplicates']) - 30} more")
    else:
        lines.append("👥 No username appears twice.")
    return lines


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(name="sheetcheck", description="Show how the bot reads the spreadsheet (tabs, tables, duplicates)")
    async def sheetcheck_command(interaction: discord.Interaction):
        if not has_required_role(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            report = await sheets.describe_sheets()
        except Exception as exc:
            await _send(interaction, f"💥 Couldn't read the spreadsheet: {exc}")
            return
        for chunk in chunk_lines("**🔎 Sheet check** — how the bot sees the spreadsheet", format_report(report)):
            await _send(interaction, chunk)
