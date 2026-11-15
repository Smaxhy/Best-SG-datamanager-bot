"""
/fix command — scan a channel, then offer to fix what the scan flagged.

Usage:  /fix channel:#rook-logs

1. Runs a normal scan (logs that are fine get their points as usual).
2. Shows a checklist of fixes: add missing players to Ancillary, update a
   renamed player's username, update wrong lore names. Everything is ticked;
   untick anything that looks wrong.
3. Apply writes the fixes, then re-scans so the fixed logs get their points.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

import config
from commands.scan import _scans_running, _send, chunk_lines, has_required_role
from services import database_service as db
from services import fix_service
from services.discord_service import run_scan

logger = logging.getLogger("bot.cmd.fix")

MAX_SELECT_OPTIONS = 25  # Discord limit per select menu
VIEW_TIMEOUT = 14 * 60   # interaction follow-ups die at 15 minutes


def _scan_line(result) -> str:
    return (
        f"Scanned **#{result.channel_name}**: {result.new_processed} new logs credited "
        f"(EP +{result.ep_added}, TP +{result.tp_added}, MP +{result.mp_added}), "
        f"{result.players_not_found} not found, {result.lore_mismatches} lore mismatches."
    )


class FixView(discord.ui.View):
    def __init__(self, invoker_id: int, channel: discord.TextChannel, fixes: list):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.invoker_id = invoker_id
        self.channel = channel
        self.fixes = fixes[:MAX_SELECT_OPTIONS]
        # Option values are list positions — two fixes can share a username
        self.selected: set[str] = {str(i) for i in range(len(self.fixes))}
        self.message: discord.Message | None = None

        select = discord.ui.Select(
            placeholder="Untick anything you don't want fixed",
            min_values=0,
            max_values=len(self.fixes),
            options=[
                discord.SelectOption(label=f.short(), value=str(i), default=True)
                for i, f in enumerate(self.fixes)
            ],
            row=0,
        )
        select.callback = self._on_select
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "❌ Only the person who ran `/fix` can use these buttons.", ephemeral=True
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected = set(self.select.values)
        for option in self.select.options:
            option.default = option.value in self.selected
        await interaction.response.defer()

    def _disable(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        self._disable()
        if self.message is not None:
            try:
                await self.message.edit(content=self.message.content + "\n\n⌛ Timed out — run `/fix` again.", view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Apply fixes", style=discord.ButtonStyle.green, row=1)
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        chosen = [f for i, f in enumerate(self.fixes) if str(i) in self.selected]
        if not chosen:
            await interaction.response.send_message("Nothing ticked — nothing to do.", ephemeral=True)
            return
        if self.channel.id in _scans_running:
            await interaction.response.send_message(
                f"⏳ A scan of {self.channel.mention} is running — try Apply again in a moment.",
                ephemeral=True,
            )
            return

        self._disable()
        self.stop()
        await interaction.response.edit_message(view=self)
        await apply_and_rescan(interaction, self.channel, chosen)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._disable()
        self.stop()
        await interaction.response.edit_message(
            content=(interaction.message.content if interaction.message else "") + "\n\n✋ Cancelled — nothing changed.",
            view=self,
        )


async def apply_and_rescan(interaction: discord.Interaction, channel: discord.TextChannel, chosen: list) -> None:
    done, failed = await fix_service.apply_fixes(chosen)

    await db.log_command(
        interaction.user.id, str(interaction.user), "fix",
        details=f"#{channel.name}: {len(done)} fixed, {len(failed)} failed — "
                + "; ".join(f.short() for f in chosen)[:400],
        channel_name=getattr(interaction.channel, "name", None),
    )

    lines = [f"✅ {line}" for line in done] + failed
    rescan = None
    if done:
        _scans_running.add(channel.id)
        try:
            rescan = await run_scan(channel, interaction.user)
        except Exception:
            logger.exception("Re-scan after /fix failed.")
            lines.append("💥 Fixes saved, but the re-scan failed — run `/scan` to credit the logs.")
        finally:
            _scans_running.discard(channel.id)

    if rescan is not None:
        lines.append("")
        lines.append("🔁 " + _scan_line(rescan))
        if rescan.flagged_messages:
            lines.append("Still flagged:")
            lines.extend(rescan.flagged_messages[:15])
            if len(rescan.flagged_messages) > 15:
                lines.append(f"… and {len(rescan.flagged_messages) - 15} more")

    for chunk in chunk_lines("**🛠️ Fix results**", lines):
        await _send(interaction, chunk)


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(name="fix", description="Scan a channel and fix missing players / wrong names it flags")
    @app_commands.describe(channel="The log channel to scan and fix")
    async def fix_command(interaction: discord.Interaction, channel: discord.TextChannel):
        if not has_required_role(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return
        if config.ALLOWED_CHANNEL_IDS and channel.id not in config.ALLOWED_CHANNEL_IDS:
            await interaction.response.send_message(
                f"❌ Channel {channel.mention} is not in the allowed scan list.", ephemeral=True
            )
            return
        if not channel.permissions_for(interaction.guild.me).read_message_history:
            await interaction.response.send_message(
                f"❌ I don't have permission to read message history in {channel.mention}.",
                ephemeral=True,
            )
            return
        if channel.id in _scans_running:
            await interaction.response.send_message(
                f"⏳ A scan of {channel.mention} is already running — wait for it to finish.",
                ephemeral=True,
            )
            return

        _scans_running.add(channel.id)
        try:
            await interaction.response.defer()
            await db.log_command(
                interaction.user.id, str(interaction.user), "fix",
                details=f"scan #{channel.name}", channel_name=interaction.channel.name,
            )
            try:
                result = await run_scan(channel, interaction.user)
            except Exception:
                logger.exception("Scan for /fix failed.")
                await _send(interaction, "💥 Scan failed due to an internal error. Check the logs.")
                return
        finally:
            _scans_running.discard(channel.id)

        plan = await fix_service.build_plan(result.issues)
        await send_plan(interaction, channel, result, plan)


async def send_plan(interaction: discord.Interaction, channel, result, plan) -> None:
    header = "**🛠️ /fix**\n" + _scan_line(result)

    if not plan.fixes and not plan.review:
        await _send(interaction, header + "\n\n✅ Nothing to fix.")
        return

    lines: list[str] = []
    if plan.fixes:
        lines.append("")
        lines.append(f"**Ready to fix ({len(plan.fixes)})** — untick any you don't want, then press **Apply**:")
        lines.extend(f"{i}. {fix.describe()}" for i, fix in enumerate(plan.fixes, 1))
        if len(plan.fixes) > MAX_SELECT_OPTIONS:
            lines.append(f"(Only the first {MAX_SELECT_OPTIONS} can be applied at once — run `/fix` again for the rest.)")
    if plan.review:
        lines.append("")
        lines.append(f"**Needs a look ({len(plan.review)})** — not changed:")
        lines.extend(plan.review)

    chunks = chunk_lines(header, lines)
    for chunk in chunks[:-1]:
        await _send(interaction, chunk)

    if not plan.fixes:
        await _send(interaction, chunks[-1])
        return

    view = FixView(interaction.user.id, channel, plan.fixes)
    msg = await interaction.followup.send(chunks[-1], view=view, wait=True)
    view.message = msg
