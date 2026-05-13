"""
Help / documentation command.

Lists every slash command the invoker can actually use in the current
guild, grouped by permission tier. Tiers are gated on Discord guild
permissions (the same ones each command's @default_permissions declares),
so a regular member only sees the public commands while a mod / admin
sees everything they can run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# Embeds cap at 25 fields and 6000 chars total; we stay well under both
# by listing each command as a single line inside one field per tier.
MAX_FIELD_VALUE = 1024


@dataclass(frozen=True)
class CommandDoc:
    name: str
    summary: str


@dataclass(frozen=True)
class Tier:
    title: str
    # Predicate against discord.Permissions. None = visible to everyone.
    check: Callable[[discord.Permissions], bool] | None
    commands: tuple[CommandDoc, ...]


TIERS: tuple[Tier, ...] = (
    Tier(
        title="📖 Everyone",
        check=None,
        commands=(
            CommandDoc("/documentation", "Show this help message."),
            CommandDoc("/userinfo", "Show detailed info about a user (mention, ID, or username)."),
            CommandDoc("/avatar", "Show a user's avatar with download links for each format."),
            CommandDoc("/ticket_open", "Open a support ticket (alternative to the panel button)."),
            CommandDoc("/ticket_close", "Close the current ticket channel (opener or staff)."),
        ),
    ),
    Tier(
        title="🛡️ Moderator — Kick Members",
        check=lambda p: p.kick_members,
        commands=(
            CommandDoc("/kick", "Kick a member from the server."),
            CommandDoc("/warn", "Warn a member (counts toward auto-kick / auto-ban thresholds)."),
            CommandDoc("/warnings", "List a user's warnings."),
            CommandDoc("/delwarn", "Delete a single warning by ID."),
        ),
    ),
    Tier(
        title="⚖️ Senior Moderator — Ban Members",
        check=lambda p: p.ban_members,
        commands=(
            CommandDoc("/ban", "Ban a user from the server (member or external user ID)."),
            CommandDoc("/unban", "Unban a user by ID."),
            CommandDoc("/clearwarnings", "Clear all warnings for a user."),
        ),
    ),
    Tier(
        title="🎟️ Staff — Manage Channels",
        check=lambda p: p.manage_channels,
        commands=(
            CommandDoc("/ticket_add", "Add a user to the current ticket channel."),
            CommandDoc("/ticket_remove", "Remove a user from the current ticket channel."),
        ),
    ),
    Tier(
        title="👑 Administrator — Manage Server",
        check=lambda p: p.manage_guild,
        commands=(
            CommandDoc("/config", "Show the current bot configuration for this server."),
            CommandDoc("/set_modlog", "Set the channel where moderation actions are logged."),
            CommandDoc("/set_honeypot", "Arm a channel as a honeypot — anyone who posts there is auto-banned."),
            CommandDoc("/clear_honeypot", "Disarm the honeypot for this server."),
            CommandDoc("/set_warn_thresholds", "Set the auto-kick and auto-ban warning thresholds."),
            CommandDoc("/automod", "Toggle the bad-language auto-filter."),
            CommandDoc("/automod_reload", "Reload the bad-words list from disk without restarting."),
            CommandDoc("/ticket_panel", "Post an Open-Ticket panel in this channel."),
            CommandDoc("/ticket_config", "Configure the ticket system (category & staff role)."),
        ),
    ),
)


def _format_tier(tier: Tier) -> str:
    """Render a tier's commands into one embed-field value, truncating if
    it would exceed Discord's 1024-char field cap (defensive — current
    content fits comfortably)."""
    lines = [f"`{cmd.name}` — {cmd.summary}" for cmd in tier.commands]
    body = "\n".join(lines)
    if len(body) <= MAX_FIELD_VALUE:
        return body
    # Trim from the end and add an ellipsis marker.
    truncated: list[str] = []
    running = 0
    suffix = "\n… (truncated)"
    budget = MAX_FIELD_VALUE - len(suffix)
    for line in lines:
        if running + len(line) + 1 > budget:
            break
        truncated.append(line)
        running += len(line) + 1
    return "\n".join(truncated) + suffix


class Documentation(commands.Cog):
    """Documentation commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="documentation",
        description="Show every command you can use, grouped by permission tier.",
    )
    @app_commands.guild_only()
    async def documentation(self, interaction: discord.Interaction) -> None:
        # Outside a guild the user has no guild perms — only the public tier shows.
        if isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
        else:
            perms = discord.Permissions.none()

        # Administrators see everything regardless of which specific bit a
        # tier checks — Discord treats administrator as implying all perms,
        # but discord.Permissions.kick_members on an admin-only role still
        # returns False, so handle it explicitly.
        is_admin = perms.administrator

        embed = discord.Embed(
            title="TheMonitorBot — Commands",
            description=(
                "Slash commands you have access to in this server. "
                "Commands you can't run are hidden."
            ),
            color=discord.Color.blurple(),
        )

        included = 0
        for tier in TIERS:
            if tier.check is not None and not is_admin and not tier.check(perms):
                continue
            embed.add_field(
                name=tier.title,
                value=_format_tier(tier),
                inline=False,
            )
            included += 1

        if included == 0:
            # Should never happen — the Everyone tier has no gate — but
            # guard so the user never sees an empty embed.
            embed.add_field(
                name="No commands available",
                value="You don't have access to any commands in this server.",
                inline=False,
            )

        embed.set_footer(text="Tip: most commands offer auto-complete in the slash UI.")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Documentation(bot))
