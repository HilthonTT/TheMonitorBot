"""
Advanced Discord moderation + ticket bot.
 
Entry point:
  • configures intents (members + presences + message_content)
  • opens the SQLite DB and attaches it as bot.db
  • registers persistent ticket button views so they survive restarts
  • auto-loads every cog in ./cogs
  • installs a global app-command error handler
  • syncs slash commands (guild-scoped if DEV_GUILD_ID is set, else global)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from data.db import Database

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

class UserInfoBot(commands.Bot):
    db: Database
    
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True              # member objects, joins, role list
        intents.presences = True
        intents.message_content = True
        
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.db = Database()
        
    async def setup_hook(self) -> None:
        await self.db.connect()
        log.info("Database connected at %s", self.db.path)
        
        # Persistent button views - must be re-added each startup.
        # Their custom_ids are stable so old panel/close messages still work.
        from cogs.tickets import TicketCloseView, TicketPanelView
        self.add_view(TicketPanelView())
        self.add_view(TicketCloseView())
        
        # Auto-load every cog in ./cogs (skip files starting with _)
        cogs_dir = Path(__file__).parent / "cogs"
        for cog_file in sorted(cogs_dir.glob("*.py")):
            if cog_file.stem.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(ext)
                log.info("Loaded extension %s", ext)
            except Exception:
                log.exception("Failed to load %s", ext)
                
        self.tree.on_error = self._on_app_command_error
        
        # Sync slash commands. Use DEV_GUILD_ID for instant updates during dev.
        guild_id = os.getenv("DEV_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d commands globally", len(synced))
            
    async def close(self) -> None:
        await self.db.close()
        await super().close()
        
    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="for trouble | /userinfo",
            ),
        )
        
    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = f"❌ Missing permission: `{', '.join(error.missing_permissions)}`"
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = f"❌ I'm missing permission: `{', '.join(error.missing_permissions)}`"
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"❌ On cooldown — try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "❌ You can't use that command here."
        else:
            log.exception("Unhandled app command error", exc_info=error)
            msg = "⚠️ Something went wrong while running that command."
            
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass
        
async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not set - see .env.example")
    
    async with UserInfoBot() as bot:
        await bot.start(token)
        
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
