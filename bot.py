"""
Discord Data Manager Bot — main entry point.

Connects to Discord, registers slash commands, initialises the DB.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord import app_commands

import config
from services.database_service import init_db
from commands import scan, stats, leaderboard, promote, say, listscan, logs, manage, deathlog, examcheck, purge, trainingban, addlog

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot")

# Suppress noisy libs
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("gspread_asyncio").setLevel(logging.WARNING)


# ── Bot setup ────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True   # required to read log messages
intents.members = True           # needed to resolve member roles


class DataManagerBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.on_app_command_error
        self._guild_commands_cleared = False

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Reply when a command crashes, instead of leaving it stuck on "thinking…"."""
        if isinstance(error, app_commands.CheckFailure):
            text = "❌ You can't use this command here."
        else:
            name = interaction.command.name if interaction.command else "command"
            logger.error("Error in /%s", name, exc_info=error)
            text = "💥 Something went wrong running that command. Please try again — if it keeps happening, check the bot logs."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            logger.warning("Could not tell the user about the error (interaction expired).")

    async def setup_hook(self) -> None:
        # Ensure data directory exists (for SQLite)
        os.makedirs("data", exist_ok=True)

        # Init database
        await init_db()

        # Register commands
        scan.register(self.tree, self)
        stats.register(self.tree, self)
        leaderboard.register(self.tree, self)
        promote.register(self.tree, self)
        say.register(self.tree, self)
        listscan.register(self.tree, self)
        logs.register(self.tree, self)
        manage.register(self.tree, self)
        deathlog.register(self.tree, self)
        examcheck.register(self.tree, self)
        purge.register(self.tree, self)
        trainingban.register(self.tree, self)
        addlog.register(self.tree, self)

        # Sync globally only
        await self.tree.sync()
        logger.info("Slash commands synced globally.")

    async def on_ready(self) -> None:
        # Clear leftover guild-specific commands (fixes duplicates).
        # on_ready fires again after every reconnect; syncing each time
        # hits Discord's rate limits, so only do it once per start.
        if not self._guild_commands_cleared:
            self._guild_commands_cleared = True
            for guild in self.guilds:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("Cleared guild commands for %s", guild.name)
        logger.info("Bot is online as %s (ID: %s)", self.user, self.user.id)


def main() -> None:
    if not config.DISCORD_TOKEN:
        logger.critical("DISCORD_TOKEN is not set. Exiting.")
        sys.exit(1)
    if not config.GOOGLE_SPREADSHEET_ID:
        logger.critical("GOOGLE_SPREADSHEET_ID is not set. Exiting.")
        sys.exit(1)

    bot = DataManagerBot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
