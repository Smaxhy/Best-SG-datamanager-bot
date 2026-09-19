"""
/listscan command — scans recent messages for ;serverbring commands
and chains all usernames into one command string.

Usage:
    /listscan channel:#event-logs
    /listscan channel:#event-logs minutes:30
"""

from __future__ import annotations

import re
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

import config

logger = logging.getLogger("bot.listscan")

# Match ";serverbring Username" — captures the username after the command
_RE_SERVERBRING = re.compile(r";serverbring\s+(\S+)", re.IGNORECASE)


def _has_permission(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    role_ids = {r.id for r in interaction.user.roles}
    return (
        config.DATA_MANAGER_ROLE_ID in role_ids
        or config.STAFF_ROLE_ID in role_ids
        or config.NCO_ROLE_ID in role_ids
    )


def register(tree: app_commands.CommandTree, bot: discord.Client) -> None:

    @tree.command(name="listscan", description="Scan for ;serverbring commands and chain all usernames")
    @app_commands.describe(
        channel="Channel to scan",
        minutes="How far back to scan (default: 15 minutes)",
    )
    async def listscan_command(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        minutes: int = 15,
    ):
        if not _has_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        perms = channel.permissions_for(interaction.guild.me)
        if not perms.read_message_history:
            await interaction.response.send_message(
                f"❌ I can't read message history in {channel.mention}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        usernames = []
        messages_checked = 0

        async for message in channel.history(after=cutoff, oldest_first=True, limit=None):
            messages_checked += 1
            
            # Check message content
            text = message.content or ""
            
            # Find all ;serverbring usernames in this message
            matches = _RE_SERVERBRING.findall(text)
            for username in matches:
                if username not in usernames:  # avoid duplicates
                    usernames.append(username)

        if not usernames:
            await interaction.followup.send(
                f"No `;serverbring` commands found in {channel.mention} "
                f"in the last {minutes} minutes ({messages_checked} messages checked)."
            )
            return

        # Build the chain
        chain = " | ".join(f";serverbring {u}" for u in usernames)

        # Discord has a 2000 char message limit — split if needed
        if len(chain) <= 1990:
            await interaction.followup.send(f"```\n{chain}\n```")
        else:
            # Split into chunks
            parts = []
            current = ""
            for u in usernames:
                entry = f";serverbring {u}"
                if current:
                    entry = " | " + entry
                if len(current) + len(entry) > 1950:
                    parts.append(current)
                    current = f";serverbring {u}"
                else:
                    current += entry
            if current:
                parts.append(current)

            for part in parts:
                await interaction.followup.send(f"```\n{part}\n```")

        # Summary
        await interaction.followup.send(
            f"✅ Found **{len(usernames)}** players in {channel.mention} "
            f"(last {minutes} min, {messages_checked} messages checked)"
        )
