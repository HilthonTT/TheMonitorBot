from __future__ import annotations

import asyncio
import logging

import discord
from async_timeout import timeout
from discord.ext import commands

from .music_utils import YTDLSource

log = logging.getLogger(__name__)


class MusicPlayer:
    """One MusicPlayer per guild — owns the queue and the playback loop."""

    __slots__ = (
        "bot",
        "_guild",
        "_channel",
        "_cog",
        "queue",
        "next",
        "current",
        "np",
        "volume",
    )

    def __init__(
        self,
        *,
        bot: commands.Bot,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        cog: commands.Cog,
    ) -> None:
        self.bot = bot
        self._guild = guild
        self._channel = channel
        self._cog = cog

        self.queue: asyncio.Queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np: discord.Message | None = None
        self.volume: float = 0.5
        self.current = None

        bot.loop.create_task(self.player_loop())

    async def player_loop(self) -> None:
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            # Wait for the next song. If we timeout, cancel player and disconnect.
            try:
                async with timeout(300):  # 5 minutes
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Stream wasn't downloaded — regather it now.
                try:
                    source = await YTDLSource.regather_stream(
                        source, loop=self.bot.loop,
                    )
                except Exception as exc:
                    log.exception("regather_stream failed")
                    await self._channel.send(
                        f":x: Sorry, I couldn't process your song.\n\n[{exc}]\n",
                        delete_after=20,
                    )
                    continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(
                source,
                after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set),
            )

            embed = discord.Embed(
                title=f"🎧 Now Playing: {source.title}",
                description=f"🎵 Requested by: **{source.requester.name}**",
                color=discord.Color.blurple(),
            )
            self.np = await self._channel.send(embed=embed)

            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            try:
                source.cleanup()
            except ValueError:
                log.debug("source.cleanup raised ValueError", exc_info=True)

            self.current = None

            if self.np is not None:
                try:
                    await self.np.delete()
                except discord.HTTPException:
                    pass
                self.np = None

    def destroy(self, guild: discord.Guild):
        """Disconnect and clean up the player."""
        return self.bot.loop.create_task(self._cog._cleanup(guild))
