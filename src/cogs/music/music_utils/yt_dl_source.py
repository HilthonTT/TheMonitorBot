from __future__ import annotations

import asyncio
from functools import partial

import discord
from yt_dlp import YoutubeDL

from ._music_utils_config import ytdl_options

ytdl = YoutubeDL(ytdl_options)


class YTDLSource(discord.PCMVolumeTransformer):
    """Wraps an FFmpeg audio source backed by yt-dlp metadata."""

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester
        self.title = data.get("title")
        self.web_url = data.get("webpage_url")

    def __getitem__(self, key: str):
        return self.__getattribute__(key)

    @classmethod
    async def create_source(
        cls,
        interaction: discord.Interaction,
        search: str,
        *,
        loop: asyncio.AbstractEventLoop,
        download: bool = False,
    ):
        """Resolve a search/URL via yt-dlp and notify the channel."""
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if "entries" in data:  # Take the first item of a playlist.
            data = data["entries"][0]

        embed = discord.Embed(
            title="🎧 Song Added to the Queue",
            description=f"🎹 {data['title']}",
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed)

        if download:
            source = ytdl.prepare_filename(data)
            return cls(
                discord.FFmpegPCMAudio(source),
                data=data,
                requester=interaction.user,
            )

        return {
            "webpage_url": data["webpage_url"],
            "requester": interaction.user,
            "title": data["title"],
        }

    @classmethod
    async def regather_stream(
        cls,
        data: dict,
        *,
        loop: asyncio.AbstractEventLoop,
    ):
        """Build a fresh stream from a stored (webpage_url, requester) dict."""
        loop = loop or asyncio.get_event_loop()
        requester = data["requester"]

        to_run = partial(ytdl.extract_info, url=data["webpage_url"], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(
            discord.FFmpegPCMAudio(data["url"]),
            data=data,
            requester=requester,
        )
