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
from commands import scan, stats

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

    async def setup_hook(self) -> None:
        # Ensure data directory exists (for SQLite)
        os.makedirs("data", exist_ok=True)

        # Init database
        await init_db()

        # Register commands
        scan.register(self.tree, self)
        stats.register(self.tree, self)

        # Sync slash commands globally (or to a specific guild for testing)
        guild_id = os.getenv("DEV_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to dev guild %s.", guild_id)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally.")

    async def on_ready(self) -> None:
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
