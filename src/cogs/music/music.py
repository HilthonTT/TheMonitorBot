"""
Music commands.

Exposes:
  /join [channel]    — bot joins a voice channel (yours by default)
  /play  <search>    — search and play a song
  /pause             — pause playback
  /resume            — resume playback
  /skip              — skip the current song
  /queue             — show the next few queued songs
  /nowplaying        — show the currently playing song
  /volume <volume>   — set playback volume (1-100)
  /stop              — clear the queue and disconnect
"""
from __future__ import annotations

import itertools
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .music_player import MusicPlayer
from .music_utils import YTDLSource

log = logging.getLogger(__name__)


class Music(commands.Cog):
    """🎵 Music commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}

    async def cog_unload(self) -> None:
        for guild_id in list(self.players):
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                await self._cleanup(guild)

    # ---------- internal helpers ----------

    async def _cleanup(self, guild: discord.Guild) -> None:
        """Disconnect from voice and drop this guild's player."""
        try:
            if guild.voice_client is not None:
                await guild.voice_client.disconnect(force=False)
        except discord.HTTPException:
            log.debug("Voice disconnect failed", exc_info=True)
        self.players.pop(guild.id, None)

    def _get_player(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
    ) -> MusicPlayer:
        player = self.players.get(guild.id)
        if player is None:
            player = MusicPlayer(bot=self.bot, guild=guild, channel=channel, cog=self)
            self.players[guild.id] = player
        return player

    async def _ensure_voice(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> Optional[discord.VoiceClient]:
        """Connect to (or move to) the requested or invoker's voice channel.

        Returns None if no channel could be resolved.
        """
        assert interaction.guild is not None
        if channel is None:
            user_voice = (
                interaction.user.voice
                if isinstance(interaction.user, discord.Member)
                else None
            )
            channel = user_voice.channel if user_voice else None
        if channel is None:
            return None

        vc = interaction.guild.voice_client
        if vc is not None:
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
            return vc  # type: ignore[return-value]
        return await channel.connect()

    @staticmethod
    def _connected_vc(
        interaction: discord.Interaction,
    ) -> Optional[discord.VoiceClient]:
        guild = interaction.guild
        if guild is None:
            return None
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            return None
        return vc  # type: ignore[return-value]

    # ---------- /join ----------
    @app_commands.command(
        name="join",
        description="Have the bot join a voice channel.",
    )
    @app_commands.describe(channel="The voice channel to join. Defaults to yours.")
    @app_commands.guild_only()
    async def join(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
        try:
            vc = await self._ensure_voice(interaction, channel)
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"❌ Failed to join voice channel: {exc}", ephemeral=True,
            )
            return

        if vc is None:
            await interaction.response.send_message(
                "❌ Join a voice channel or specify one.", ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🎧 Connected",
            description=f"```🎶 Channel: {vc.channel.name}```",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="❓ Use /stop to disconnect me at any time.")
        await interaction.response.send_message(embed=embed)

    # ---------- /play ----------
    @app_commands.command(
        name="play",
        description="Search and play a song in a voice channel.",
    )
    @app_commands.describe(search="A song name or URL.")
    @app_commands.guild_only()
    async def play(
        self,
        interaction: discord.Interaction,
        search: str,
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert interaction.guild is not None

        try:
            vc = await self._ensure_voice(interaction)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Failed to join voice channel: {exc}", ephemeral=True,
            )
            return
        if vc is None:
            await interaction.followup.send(
                "❌ Join a voice channel first.", ephemeral=True,
            )
            return

        assert interaction.channel is not None
        player = self._get_player(interaction.guild, interaction.channel)
        try:
            source = await YTDLSource.create_source(
                interaction, search, loop=self.bot.loop, download=False,
            )
        except Exception as exc:
            log.exception("YTDL failed for search %r", search)
            await interaction.followup.send(
                f"❌ Couldn't fetch that song: {exc}", ephemeral=True,
            )
            return

        await player.queue.put(source)

    # ---------- /pause ----------
    @app_commands.command(name="pause", description="Pause the current song.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        vc = self._connected_vc(interaction)
        if vc is None or not vc.is_playing():
            await interaction.response.send_message(
                "❌ I'm not currently playing anything.", ephemeral=True,
            )
            return
        if vc.is_paused():
            await interaction.response.send_message(
                "❌ Already paused.", ephemeral=True,
            )
            return
        vc.pause()
        embed = discord.Embed(
            title="🎧 Paused",
            description=f"⏸️ Paused by **{interaction.user.name}**",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    # ---------- /resume ----------
    @app_commands.command(name="resume", description="Resume playback.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        vc = self._connected_vc(interaction)
        if vc is None:
            await interaction.response.send_message(
                "❌ I'm not connected to voice.", ephemeral=True,
            )
            return
        if not vc.is_paused():
            await interaction.response.send_message(
                "❌ I'm not paused.", ephemeral=True,
            )
            return
        vc.resume()
        embed = discord.Embed(
            title="🎧 Resumed",
            description=f"▶️ Resumed by **{interaction.user.name}**",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    # ---------- /skip ----------
    @app_commands.command(name="skip", description="Skip the current song.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        vc = self._connected_vc(interaction)
        if vc is None or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message(
                "❌ I'm not currently playing anything.", ephemeral=True,
            )
            return
        vc.stop()
        embed = discord.Embed(
            title="🎧 Skipped",
            description=f"⏭️ Skipped by **{interaction.user.name}**",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    # ---------- /queue ----------
    @app_commands.command(
        name="queue",
        description="Show the next songs in the queue.",
    )
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        if self._connected_vc(interaction) is None:
            await interaction.response.send_message(
                "❌ I'm not connected to voice.", ephemeral=True,
            )
            return

        player = self.players.get(interaction.guild.id)
        if player is None or player.queue.empty():
            await interaction.response.send_message(
                "❌ There are no more queued songs.", ephemeral=True,
            )
            return

        upcoming = list(itertools.islice(player.queue._queue, 0, 5))
        fmt = "\n\n".join(
            f"➡️ **{i + 1}**: {song['title']}" for i, song in enumerate(upcoming)
        )
        embed = discord.Embed(
            title=f"🎧 Music Queue | {len(upcoming)} Songs",
            description=fmt,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="❓ Use /skip to jump to the next song.")
        await interaction.response.send_message(embed=embed)

    # ---------- /nowplaying ----------
    @app_commands.command(
        name="nowplaying",
        description="Show the song that's currently playing.",
    )
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        vc = self._connected_vc(interaction)
        player = self.players.get(interaction.guild.id)
        if vc is None or player is None or player.current is None or vc.source is None:
            await interaction.response.send_message(
                "❌ I'm not currently playing anything.", ephemeral=True,
            )
            return

        if player.np is not None:
            try:
                await player.np.delete()
            except discord.HTTPException:
                pass

        embed = discord.Embed(
            title=f"🎧 Now Playing: {vc.source.title}",
            description=f"🎵 Requested by: **{vc.source.requester.name}**",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)
        player.np = await interaction.original_response()

    # ---------- /volume ----------
    @app_commands.command(
        name="volume",
        description="Set the player volume (1-100).",
    )
    @app_commands.describe(volume="Playback volume between 1 and 100.")
    @app_commands.guild_only()
    async def volume(
        self,
        interaction: discord.Interaction,
        volume: app_commands.Range[int, 1, 100],
    ) -> None:
        assert interaction.guild is not None
        vc = self._connected_vc(interaction)
        if vc is None:
            await interaction.response.send_message(
                "❌ I'm not connected to voice.", ephemeral=True,
            )
            return

        player = self.players.get(interaction.guild.id)
        if vc.source is not None:
            vc.source.volume = volume / 100
        if player is not None:
            player.volume = volume / 100

        embed = discord.Embed(
            title="🎧 Volume Changed",
            description=f"🔊 **{interaction.user.name}** set the volume to *{volume}%*",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    # ---------- /stop ----------
    @app_commands.command(
        name="stop",
        description="Clear the queue and disconnect from voice.",
    )
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        if self._connected_vc(interaction) is None:
            await interaction.response.send_message(
                "❌ I'm not connected to voice.", ephemeral=True,
            )
            return
        await self._cleanup(interaction.guild)
        await interaction.response.send_message("👋 Disconnected.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
