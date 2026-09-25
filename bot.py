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
        # Clear leftover guild-specific commands (fixes duplicates)
        for guild in self.guilds:
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Cleared guild commands for %s", guild.name)
        logger.info("Bot is online as %s (ID: %s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        # Ignore bot's own messages
        if message.author == self.user:
            return

        # Auto-reply when someone DIRECTLY pings Miyabi (not replies)
        MIYABI_ID = 621085757897179142
        if message.reference is not None:
            return  # skip replies
        mentioned = any(m.id == MIYABI_ID for m in message.mentions)
        if not mentioned:
            mentioned = f"<@{MIYABI_ID}>" in message.content or f"<@!{MIYABI_ID}>" in message.content
        if mentioned:
            await message.reply(
                "*Miyabi Cruz, known within the walls as the recruit finger cutter. "
                "Recruits fear her as she is upmost evil.*\n"
                "https://cdn.discordapp.com/attachments/1329925898094514176/1552814006538993826/"
                "image_2026-09-24_174804547.gif?ex=6ab6fa3d&is=6ab5a8bd&"
                "hm=920e09d8d07ffde8b897fb9ec32482624fe164c2f5902f006f6d4e4575e47779&"
            )
            return

        # Auto-reply when someone DIRECTLY pings user 781794464023969812
        SMAXHY_ID = 781794464023969812
        mentioned2 = any(m.id == SMAXHY_ID for m in message.mentions)
        if not mentioned2:
            mentioned2 = f"<@{SMAXHY_ID}>" in message.content or f"<@!{SMAXHY_ID}>" in message.content
        if mentioned2:
            await message.reply(
                "https://cdn.discordapp.com/attachments/1549886191501840514/1552812092489211924/"
                "image_2026-09-24_174030285.gif?ex=6ab7a134&is=6ab64fb4&"
                "hm=bb2ac8b93b98072267a264be9cb0da594368ee53b16184fa0398a03dc32d1cd3&"
            )


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
