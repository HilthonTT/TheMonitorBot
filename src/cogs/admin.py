"""
Per-guild configuration commands.
 
  /set_modlog          choose channel for moderation embeds
  /set_honeypot        arm a channel as a spam-bot honeypot
  /clear_honeypot      disarm the honeypot
  /set_warn_thresholds tune auto-kick / auto-ban warning thresholds
  /config              show the current configuration
 
All commands require Manage Server.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from data.db import Database

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        
    @property
    def db(self) -> Database:
        return self.bot.db  # type: ignore[attr-defined]
    
    @app_commands.command(
        name="set_modlog",
        description="Set the channel where moderation actions are logged.",
    )
    @app_commands.describe(channel="The channel to use as the mod log.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_modlog(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return
        
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.mod_log_channel_id = channel.id
        await self.db.upsert_config(cfg)
        await interaction.response.send_message(
            f"✅ Mod log set to {channel.mention}", ephemeral=True,
        )
        
    @app_commands.command(
        name="set_honeypot",
        description="Arm a channel as a honeypot — anyone who posts there is auto-banned.",
    )
    @app_commands.describe(channel="The honeypot channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_honeypot(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return
        
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.honeypot_channel_id = channel.id
        await self.db.upsert_config(cfg)
        await interaction.response.send_message(
            f"🍯 Honeypot armed on {channel.mention}.\n"
            f"⚠️ For best results: deny `View Channel` on @everyone, "
            f"hide it from search, and never link to it. Spam bots that "
            f"scrape every readable channel will still find it via the API.",
            ephemeral=True,
        )    