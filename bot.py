import logging
import sys

import discord
from discord.ext import commands

from notebookbot import config
from notebookbot.cog import NotebookCog
from notebookbot.nlm import NotebookService
from notebookbot.store import Store

log = logging.getLogger("notebookbot")


class NotebookBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            activity=discord.Activity(type=discord.ActivityType.listening, name="/ask"),
        )
        self.service = NotebookService(config.NOTEBOOKLM_PROFILE)
        self.store = Store(config.DATA_DIR)

    async def setup_hook(self) -> None:
        try:
            await self.service.start()
        except Exception:
            log.exception(
                "Couldn't log in to NotebookLM. Run `.venv/bin/notebooklm login` with the bot's "
                "Google account first (see README.md)."
            )
            await self.close()
            return
        email = await self.service.account_email()
        log.info("Connected to NotebookLM as %s", email or "(unknown account)")

        await self.add_cog(NotebookCog(self, self.service, self.store))
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                log.error(
                    "Couldn't register slash commands in server %s. Check that GUILD_ID is the "
                    "server's ID and that the bot was invited with the 'bot' and "
                    "'applications.commands' scopes (see README.md).",
                    config.GUILD_ID,
                )
                await self.close()
                return
            log.info("Synced %d commands to guild %s", len(synced), config.GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands (may take up to an hour to show up)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        await super().close()
        await self.service.close()


def main() -> None:
    discord.utils.setup_logging()
    if not config.DISCORD_TOKEN:
        sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    NotebookBot().run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
