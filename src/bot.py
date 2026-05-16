"""
Advanced Discord moderation + ticket bot.

Entry point:
  • loads .env BEFORE importing project modules so BOT_DB_PATH is honoured
  • configures intents (members + presences + message_content)
  • opens the SQLite DB and attaches it as bot.db
  • registers persistent ticket button views so they survive restarts
  • auto-loads every cog in ./cogs
  • installs a global app-command error handler
  • handles SIGTERM for graceful shutdown under Docker / systemd
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE any project import that may read env at import time
# (e.g. data.db used to resolve BOT_DB_PATH on import).
load_dotenv()

import discord                                # noqa: E402
from discord import app_commands              # noqa: E402
from discord.ext import commands              # noqa: E402

from data.db import Database                  # noqa: E402

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")


def _parse_int_env(name: str) -> int | None:
    """Parse an optional integer env var. Exit on malformed value."""
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got: {raw!r}")


class MonitorBot(commands.Bot):
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

        # Persistent button views - must be re-added each startup.
        # Their custom_ids are stable so old panel/close messages still work.
        from cogs.tickets import TicketCloseView, TicketPanelView
        self.add_view(TicketPanelView())
        self.add_view(TicketCloseView())

        # Auto-load every cog in ./cogs (skip files/dirs starting with _).
        # Top-level *.py files load as cogs.<stem>.
        # Sub-packages load as cogs.<dir>.<dir> if <dir>/<dir>.py exists
        # (e.g. cogs/music/music.py -> cogs.music.music).
        cogs_dir = Path(__file__).parent / "cogs"
        extensions: list[str] = []
        for cog_file in sorted(cogs_dir.glob("*.py")):
            if cog_file.stem.startswith("_"):
                continue
            extensions.append(f"cogs.{cog_file.stem}")
        for sub in sorted(p for p in cogs_dir.iterdir() if p.is_dir()):
            if sub.name.startswith("_") or sub.name == "__pycache__":
                continue
            entry = sub / f"{sub.name}.py"
            if entry.is_file():
                extensions.append(f"cogs.{sub.name}.{sub.name}")

        for ext in extensions:
            try:
                await self.load_extension(ext)
                log.info("Loaded extension %s", ext)
            except Exception:
                log.exception("Failed to load %s", ext)

        self.tree.on_error = self._on_app_command_error

        # Sync slash commands. Use DEV_GUILD_ID for instant updates during dev.
        guild_id = _parse_int_env("DEV_GUILD_ID")
        if guild_id is not None:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d commands globally", len(synced))

    async def close(self) -> None:
        await super().close()
        await self.db.close()

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)

        activity_status = os.getenv("ACTIVITY_STATUS") or "Always monitoring your behavior"

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=activity_status,
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
        elif isinstance(error, app_commands.NoPrivateMessage):
            msg = "❌ That command can only be used in a server."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "❌ You can't use that command here."
        else:
            log.exception(
                "Unhandled app command error",
                exc_info=error,
                extra={
                    "guild_id": interaction.guild_id,
                    "user_id": interaction.user.id if interaction.user else None,
                    "command": interaction.command.qualified_name if interaction.command else None,
                },
            )
            msg = "⚠️ Something went wrong while running that command."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            log.debug("Failed to send error reply", exc_info=True)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, bot: MonitorBot) -> None:
    """Best-effort SIGTERM/SIGINT handler for graceful shutdown.

    add_signal_handler is POSIX-only; on Windows we fall back to the
    default KeyboardInterrupt path.
    """
    def _request_close() -> None:
        log.info("Signal received, closing bot…")
        loop.create_task(bot.close())

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_close)
        except (NotImplementedError, RuntimeError):
            # Windows: signals beyond SIGINT aren't supported in asyncio.
            pass


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not set — see .env.example")

    loop = asyncio.get_running_loop()
    async with MonitorBot() as bot:
        _install_signal_handlers(loop, bot)
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
        sys.exit(0)
