"""The Discord client and its lifecycle."""

from __future__ import annotations

import discord
import structlog
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .db.session import create_engine, create_session_factory
from .registrar import RegistrarClient
from .services.notifier import Notifier
from .services.scheduler import ScraperService, bootstrap
from .ui.embeds import about_embed, error_embed

log = structlog.get_logger(__name__)

COGS = (
    "bruinwatch.cogs.search",
    "bruinwatch.cogs.watchlist",
    "bruinwatch.cogs.aliases",
    "bruinwatch.cogs.admin",
)


class BruinWatchBot(commands.Bot):
    """Slash-command only, so no privileged message-content intent is needed."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            # command_prefix is required by commands.Bot but unused: every
            # command is an application command.
            command_prefix=commands.when_mentioned,
            # `guilds` is the only intent needed, and it is not privileged: it
            # gets us on_guild_join and a populated `bot.guilds`. Notably absent
            # is `message_content`, which slash commands make unnecessary.
            intents=discord.Intents(guilds=True),
            help_command=None,
            owner_id=settings.owner_id,
        )
        self.settings = settings
        self.engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            single_connection=settings.db_single_connection,
        )
        self.sessions: async_sessionmaker[AsyncSession] = create_session_factory(self.engine)
        self.registrar = RegistrarClient(
            user_agent=settings.user_agent,
            max_concurrency=settings.max_concurrency,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
        )
        self.notifier = Notifier(self, self.sessions)
        self.scraper = ScraperService(
            settings, self.sessions, self.registrar, on_alert=self.alert_owner
        )

    async def setup_hook(self) -> None:
        """Runs once, before login completes. Cogs must be added here in 2.x."""
        for cog in COGS:
            await self.load_extension(cog)

        if self.settings.dev_guild_id is not None:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("synced_commands", scope="guild", guild_id=self.settings.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("synced_commands", scope="global")

        # Seed terms and subject areas so autocomplete works on first boot.
        try:
            await bootstrap(self.registrar, self.sessions)
        except Exception:
            log.exception("bootstrap_failed")

        self.notifier.start()
        if self.settings.scheduler_enabled:
            self.scraper.start()
        else:
            log.warning("scheduler_disabled")

    async def on_ready(self) -> None:
        log.info("ready", user=str(self.user), guilds=len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="UCLA enrollment")
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None,
            )
        if channel is not None:
            await channel.send(embed=about_embed())

    async def alert_owner(self, message: str) -> None:
        if self.settings.owner_id is None:
            return
        try:
            owner = self.get_user(self.settings.owner_id) or await self.fetch_user(
                self.settings.owner_id
            )
            await owner.send(embed=error_embed(message))
        except discord.HTTPException:
            log.warning("owner_alert_failed")

    async def close(self) -> None:
        self.scraper.shutdown()
        await self.notifier.stop()
        await self.registrar.aclose()
        await super().close()
        await self.engine.dispose()
