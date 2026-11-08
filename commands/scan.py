"""
/scan command — triggers a channel scan for player activity logs.

Usage:  /scan channel:#rook-logs
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

import config
from services.discord_service import run_scan
from services import database_service as db

logger = logging.getLogger("bot.cmd.scan")

# Channels with a scan in progress. Two scans of the same channel at once
# would both see a log as "not processed yet" and add its points twice.
_scans_running: set[int] = set()

DISCORD_MESSAGE_LIMIT = 2000


def chunk_lines(header: str, lines: list[str], limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Join lines into as few messages as possible, each under Discord's length limit."""
    chunks: list[str] = []
    current = header
    for line in lines:
        if len(line) > limit - 1:
            line = line[: limit - 2] + "…"
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _send(interaction: discord.Interaction, content: str | None = None, **kwargs) -> None:
    """
    Reply via the interaction follow-up. Discord only allows that for 15 minutes,
    so if a long scan outlives it, post in the channel instead of losing the result.
    """
    try:
        await interaction.followup.send(content, **kwargs)
    except discord.HTTPException as exc:
        # 404 Unknown Webhook / 401 Invalid Webhook Token = the interaction expired
        if not isinstance(exc, discord.NotFound) and exc.status != 401:
            raise
        logger.warning("Interaction expired, posting scan result in channel instead.")
        if content is not None:
            content = f"{interaction.user.mention} {content}"
        else:
            content = interaction.user.mention
        await interaction.channel.send(content, **kwargs)


def has_required_role(interaction: discord.Interaction) -> bool:
    """Check the invoker has Data Manager or Staff role."""
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in interaction.user.roles}
    return (
        config.DATA_MANAGER_ROLE_ID in role_ids
        or config.STAFF_ROLE_ID in role_ids
        or config.NCO_ROLE_ID in role_ids
    )


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:
    """Register the /scan command on the provided tree."""

    @tree.command(name="scan", description="Scan a channel for player activity logs")
    @app_commands.describe(
        channel="The Discord channel to scan",
        exclude="Usernames to skip this scan (comma-separated, e.g. Smaxhy,foot3331)",
        force="Force rescan — ignore already-processed messages",
    )
    async def scan_command(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        exclude: str | None = None,
        force: bool = False,
    ):
        # ── Permission check ─────────────────────────────────────
        if not has_required_role(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        # ── Channel restriction (optional) ───────────────────────
        if config.ALLOWED_CHANNEL_IDS and channel.id not in config.ALLOWED_CHANNEL_IDS:
            await interaction.response.send_message(
                f"❌ Channel {channel.mention} is not in the allowed scan list.",
                ephemeral=True,
            )
            return

        # ── Bot must be able to read the channel ─────────────────
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.read_message_history:
            await interaction.response.send_message(
                f"❌ I don't have permission to read message history in {channel.mention}.",
                ephemeral=True,
            )
            return

        # ── One scan per channel at a time ───────────────────────
        if channel.id in _scans_running:
            await interaction.response.send_message(
                f"⏳ A scan of {channel.mention} is already running — wait for it to finish.",
                ephemeral=True,
            )
            return

        _scans_running.add(channel.id)
        try:
            # ── Acknowledge (we may take a while) ────────────────
            await interaction.response.defer(ephemeral=False)
            await _scan(interaction, channel, exclude, force)
        finally:
            _scans_running.discard(channel.id)


async def _scan(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    exclude: str | None,
    force: bool,
) -> None:
    await db.log_command(
        interaction.user.id, str(interaction.user), "scan",
        details=f"#{channel.name}", channel_name=interaction.channel.name,
    )

    # Parse excluded usernames
    excluded_names: set[str] = set()
    if exclude:
        excluded_names = {name.strip().lower() for name in exclude.split(",") if name.strip()}

    try:
        result = await run_scan(channel, interaction.user, excluded_names=excluded_names, force=force)
    except Exception:
        logger.exception("Scan failed.")
        await _send(interaction, "💥 Scan failed due to an internal error. Check the logs.")
        return

    # ── Fetch DM's current weekly score ──────────────────────
    from services.database_service import get_dm_current_week
    dm_week = await get_dm_current_week(interaction.user.id)
    weekly_total = dm_week.logs_processed if dm_week else 0

    # ── Build summary embed ──────────────────────────────────
    embed = discord.Embed(
        title="📊 Scan Complete",
        colour=discord.Colour.green() if result.errors == 0 else discord.Colour.orange(),
    )
    embed.add_field(name="Channel", value=f"#{result.channel_name}", inline=True)
    embed.add_field(name="Time range", value=f"Last {config.SCAN_HOURS} hours", inline=True)
    embed.add_field(name="Data Manager", value=interaction.user.mention, inline=True)
    embed.add_field(name="Messages checked", value=str(result.messages_checked), inline=True)
    embed.add_field(name="Valid logs found", value=str(result.valid_logs), inline=True)
    embed.add_field(name="New logs processed", value=str(result.new_processed), inline=True)
    embed.add_field(name="Already processed", value=str(result.already_processed), inline=True)
    embed.add_field(name="Players updated", value=str(result.players_updated), inline=True)
    embed.add_field(name="Players not found", value=str(result.players_not_found), inline=True)
    embed.add_field(name="Invalid logs", value=str(result.invalid_logs), inline=True)
    embed.add_field(name="Lore mismatches", value=str(result.lore_mismatches), inline=True)
    embed.add_field(name="Errors", value=str(result.errors), inline=True)
    embed.add_field(name="EP added", value=f"+{result.ep_added}", inline=True)
    embed.add_field(name="TP added", value=f"+{result.tp_added}", inline=True)
    embed.add_field(name="MP added", value=f"+{result.mp_added}", inline=True)
    embed.add_field(
        name="Your weekly score",
        value=f"**{weekly_total}** logs this week",
        inline=False,
    )

    await _send(interaction, embed=embed)

    # ── Flagged items (sent separately so the embed stays clean)
    if result.flagged_messages:
        lines = result.flagged_messages[:20]
        if len(result.flagged_messages) > 20:
            lines.append(f"… and {len(result.flagged_messages) - 20} more")
        for chunk in chunk_lines("**Flagged items:**", lines):
            await _send(interaction, chunk)
