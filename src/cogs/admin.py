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


def _is_manager(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )


def _invalidate_automod(bot: commands.Bot, guild_id: int) -> None:
    """Best-effort invalidation of AutoMod's per-guild config cache."""
    automod = bot.get_cog("AutoMod")
    if automod is not None and hasattr(automod, "invalidate"):
        automod.invalidate(guild_id)  # type: ignore[attr-defined]


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self) -> Database:
        return self.bot.db  # type: ignore[attr-defined]

    # ---------- /set_modlog ----------
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
        if not _is_manager(interaction):
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.mod_log_channel_id = channel.id
        await self.db.upsert_config(cfg)
        _invalidate_automod(self.bot, interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Mod log set to {channel.mention}", ephemeral=True,
        )

    # ---------- /set_honeypot ----------
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
        if not _is_manager(interaction):
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.honeypot_channel_id = channel.id
        await self.db.upsert_config(cfg)
        _invalidate_automod(self.bot, interaction.guild.id)
        await interaction.response.send_message(
            f"🍯 Honeypot armed on {channel.mention}.\n"
            f"⚠️ For best results: deny `View Channel` on @everyone, "
            f"hide it from search, and never link to it. Spam bots that "
            f"scrape every readable channel will still find it via the API.",
            ephemeral=True,
        )

    # ---------- /clear_honeypot ----------
    @app_commands.command(
        name="clear_honeypot",
        description="Disarm the honeypot for this server.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def clear_honeypot(self, interaction: discord.Interaction) -> None:
        if not _is_manager(interaction):
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.honeypot_channel_id = None
        await self.db.upsert_config(cfg)
        _invalidate_automod(self.bot, interaction.guild.id)
        await interaction.response.send_message(
            "✅ Honeypot disarmed.", ephemeral=True,
        )

    # ---------- /set_warn_thresholds ----------
    @app_commands.command(
        name="set_warn_thresholds",
        description="Set the auto-kick and auto-ban warning thresholds.",
    )
    @app_commands.describe(
        kick_threshold="Number of warnings that triggers an auto-kick (>= 1).",
        ban_threshold="Number of warnings that triggers an auto-ban (>= 1).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_warn_thresholds(
        self,
        interaction: discord.Interaction,
        kick_threshold: app_commands.Range[int, 1, 100],
        ban_threshold: app_commands.Range[int, 1, 100],
    ) -> None:
        if not _is_manager(interaction):
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return

        if ban_threshold < kick_threshold:
            await interaction.response.send_message(
                "❌ Ban threshold must be ≥ kick threshold.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.warn_kick_threshold = kick_threshold
        cfg.warn_ban_threshold = ban_threshold
        await self.db.upsert_config(cfg)
        _invalidate_automod(self.bot, interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Auto-kick at **{kick_threshold}** warning(s); "
            f"auto-ban at **{ban_threshold}**.",
            ephemeral=True,
        )

    # ---------- /config ----------
    @app_commands.command(
        name="config",
        description="Show the current bot configuration for this server.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def show_config(self, interaction: discord.Interaction) -> None:
        if not _is_manager(interaction):
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        cfg = await self.db.get_config(interaction.guild.id)

        def _chan(id_: int | None) -> str:
            if id_ is None:
                return "—"
            chan = interaction.guild.get_channel(id_) if interaction.guild else None
            return chan.mention if chan else f"`{id_}` (missing)"

        def _role(id_: int | None) -> str:
            if id_ is None:
                return "—"
            role = interaction.guild.get_role(id_) if interaction.guild else None
            return role.mention if role else f"`{id_}` (missing)"

        embed = discord.Embed(
            title=f"Configuration — {interaction.guild.name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Mod log", value=_chan(cfg.mod_log_channel_id), inline=True)
        embed.add_field(name="Honeypot", value=_chan(cfg.honeypot_channel_id), inline=True)
        embed.add_field(name="Staff role", value=_role(cfg.staff_role_id), inline=True)
        embed.add_field(
            name="Ticket category", value=_chan(cfg.ticket_category_id), inline=True,
        )
        embed.add_field(
            name="Auto-kick threshold", value=str(cfg.warn_kick_threshold), inline=True,
        )
        embed.add_field(
            name="Auto-ban threshold", value=str(cfg.warn_ban_threshold), inline=True,
        )
        embed.add_field(
            name="Automod",
            value="enabled" if cfg.automod_enabled else "disabled",
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
