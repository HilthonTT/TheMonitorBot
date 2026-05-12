"""
Moderation: kick, ban, unban, warn, warning history.

All commands run hierarchy & self-protection checks, DM the user before
applying action (best-effort), and post a structured embed to the configured
mod-log channel if one is set.

Permissions:
  /kick, /warn, /warnings, /delwarn -> Kick Members
  /ban,  /unban, /clearwarnings     -> Ban Members
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from data.db import Database, GuildConfig

log = logging.getLogger(__name__)

# --- Tuning constants ---
AUDIT_REASON_MAX = 512        # Discord audit-log reason cap
WARN_REASON_MAX = 1_000       # per-warning embed field cap
WARNINGS_PAGE_SIZE = 25       # Discord embed field cap


def _audit_reason(moderator: discord.abc.User, reason: str) -> str:
    """Build an audit-log reason within Discord's 512-char limit."""
    return f"By {moderator} — {reason}"[:AUDIT_REASON_MAX]


def _safety_check(
    interaction: discord.Interaction, target: discord.Member,
) -> str | None:
    """Return an error string if the action should be refused, else None."""
    assert interaction.guild is not None
    if target.id == interaction.user.id:
        return "You cannot moderate yourself."
    if target.id == interaction.guild.owner_id:
        return "You cannot moderate the server owner."
    if interaction.client.user is not None and target.id == interaction.client.user.id:
        return "I cannot moderate myself."

    me = interaction.guild.me
    if me is not None and target.top_role >= me.top_role:
        return "I cannot moderate someone whose top role is at or above mine."

    if (
        isinstance(interaction.user, discord.Member)
        and interaction.user.id != interaction.guild.owner_id
        and target.top_role >= interaction.user.top_role
    ):
        return "You cannot moderate someone whose top role is at or above yours."
    return None


async def send_modlog(
    bot: commands.Bot, guild: discord.Guild, embed: discord.Embed,
) -> None:
    cfg = await bot.db.get_config(guild.id)  # type: ignore[attr-defined]
    if cfg.mod_log_channel_id is None:
        return
    chan = guild.get_channel(cfg.mod_log_channel_id)
    if isinstance(chan, discord.TextChannel):
        try:
            await chan.send(embed=embed)
        except discord.HTTPException:
            log.warning("Failed to send mod log to %s", chan.id, exc_info=True)


def action_embed(
    action: str,
    color: discord.Color,
    target: discord.abc.User,
    moderator: discord.abc.User,
    reason: str,
    extra: dict[str, str] | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"User {action}",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(
        name="User",
        value=f"{target.mention}\n`{target}` (`{target.id}`)",
        inline=False,
    )
    embed.add_field(name="Moderator", value=moderator.mention, inline=True)
    embed.add_field(name="Reason", value=reason or "—", inline=False)
    if extra:
        for k, v in extra.items():
            embed.add_field(name=k, value=v, inline=True)
    return embed


def _require_member(interaction: discord.Interaction) -> discord.Member | None:
    """Return interaction.user as Member, or None if invoked outside a guild."""
    if isinstance(interaction.user, discord.Member):
        return interaction.user
    return None


class Moderation(commands.Cog):
    """Moderation commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self) -> Database:
        return self.bot.db  # type: ignore[attr-defined]

    # ---------- /kick ----------
    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="The member to kick.", reason="Why you're kicking them.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = None,
    ) -> None:
        member = _require_member(interaction)
        if member is None or not member.guild_permissions.kick_members:
            await interaction.response.send_message(
                "❌ You need the **Kick Members** permission.", ephemeral=True,
            )
            return

        if (err := _safety_check(interaction, user)) is not None:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return

        assert interaction.guild is not None
        reason = reason or "No reason provided"

        # Defer first — DM + REST call may exceed 3s ack window.
        await interaction.response.defer(thinking=True)

        try:
            await user.send(
                f"You have been kicked from **{interaction.guild.name}**.\nReason: {reason}",
            )
        except discord.HTTPException:
            log.debug("Could not DM kick target %s", user.id, exc_info=True)

        try:
            await user.kick(reason=_audit_reason(interaction.user, reason))
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I lack permission to kick that user.", ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Discord error: {e}", ephemeral=True)
            return

        embed = action_embed("Kicked", discord.Color.orange(), user, interaction.user, reason)
        await interaction.followup.send(embed=embed)
        await send_modlog(self.bot, interaction.guild, embed)

    @app_commands.command(name="ban", description="Ban a user from the server.")
    @app_commands.describe(
        user="The user to ban (member or external user ID).",
        reason="Why you're banning them.",
        delete_message_days="Days of recent messages to delete (0–7).",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: Optional[str] = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        member = _require_member(interaction)
        if member is None or not member.guild_permissions.ban_members:
            await interaction.response.send_message(
                "❌ You need the **Ban Members** permission.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        target_member = interaction.guild.get_member(user.id)
        if target_member is not None:
            if (err := _safety_check(interaction, target_member)) is not None:
                await interaction.response.send_message(f"❌ {err}", ephemeral=True)
                return

        await interaction.response.defer(thinking=True)
        reason = reason or "No reason provided"

        if target_member is not None:
            try:
                await target_member.send(
                    f"You have been banned from **{interaction.guild.name}**.\n"
                    f"Reason: {reason}",
                )
            except discord.HTTPException:
                log.debug("Could not DM ban target %s", user.id, exc_info=True)

        try:
            await interaction.guild.ban(
                user,
                reason=_audit_reason(interaction.user, reason),
                # `delete_message_days` was deprecated in discord.py 2.1.
                delete_message_seconds=delete_message_days * 86_400,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I lack permission to ban that user.", ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Discord error: {e}", ephemeral=True)
            return

        embed = action_embed(
            "Banned", discord.Color.red(), user, interaction.user, reason,
            extra={"Message Cleanup": f"{delete_message_days}d"},
        )
        await interaction.followup.send(embed=embed)
        await send_modlog(self.bot, interaction.guild, embed)

    # ---------- /unban ----------
    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.describe(user_id="The numeric ID of the banned user.", reason="Why.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(2, 5.0, key=lambda i: i.user.id)
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: Optional[str] = None,
    ) -> None:
        member = _require_member(interaction)
        if member is None or not member.guild_permissions.ban_members:
            await interaction.response.send_message(
                "❌ You need the **Ban Members** permission.", ephemeral=True,
            )
            return
        if not user_id.isdigit():
            await interaction.response.send_message(
                "❌ User ID must be numeric.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        await interaction.response.defer(thinking=True)

        try:
            user = await self.bot.fetch_user(int(user_id))
        except discord.NotFound:
            await interaction.followup.send("❌ User not found.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Discord error: {e}", ephemeral=True)
            return

        try:
            await interaction.guild.unban(
                user, reason=_audit_reason(interaction.user, reason or "—"),
            )
        except discord.NotFound:
            await interaction.followup.send(
                "❌ That user is not banned.", ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I lack permission to unban that user.", ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Discord error: {e}", ephemeral=True)
            return

        embed = action_embed(
            "Unbanned", discord.Color.green(), user, interaction.user, reason or "—",
        )
        await interaction.followup.send(embed=embed)
        await send_modlog(self.bot, interaction.guild, embed)

    # ---------- /warn ----------
    @app_commands.command(name="warn", description="Warn a member.")
    @app_commands.describe(user="The member to warn.", reason="Why you're warning them.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str,
    ) -> None:
        member = _require_member(interaction)
        if member is None or not member.guild_permissions.kick_members:
            await interaction.response.send_message(
                "❌ You need the **Kick Members** permission.", ephemeral=True,
            )
            return
        if (err := _safety_check(interaction, user)) is not None:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return

        assert interaction.guild is not None
        await interaction.response.defer(thinking=True)

        cfg = await self.db.get_config(interaction.guild.id)
        wid = await self.db.add_warning(
            interaction.guild.id, user.id, interaction.user.id, reason,
        )
        warns = await self.db.get_warnings(interaction.guild.id, user.id)
        count = len(warns)

        try:
            await user.send(
                f"⚠️ You have been warned in **{interaction.guild.name}**.\n"
                f"Reason: {reason}\nTotal warnings: **{count}**",
            )
        except discord.HTTPException:
            log.debug("Could not DM warn target %s", user.id, exc_info=True)

        embed = action_embed(
            "Warned", discord.Color.gold(), user, interaction.user, reason,
            extra={"Warning #": str(wid), "Total": str(count)},
        )
        await interaction.followup.send(embed=embed)
        await send_modlog(self.bot, interaction.guild, embed)

        await self.maybe_escalate(interaction.guild, user, count, cfg)

    async def maybe_escalate(
        self,
        guild: discord.Guild,
        user: discord.Member,
        count: int,
        cfg: GuildConfig,
    ) -> None:
        """Auto-kick or auto-ban once configured thresholds are crossed."""
        try:
            if count >= cfg.warn_ban_threshold:
                reason = (
                    f"Auto-ban: reached {count} warnings "
                    f"(threshold {cfg.warn_ban_threshold})"
                )
                try:
                    await user.send(f"You were auto-banned from **{guild.name}**: {reason}")
                except discord.HTTPException:
                    log.debug("Could not DM auto-ban target", exc_info=True)
                await guild.ban(user, reason=reason[:AUDIT_REASON_MAX], delete_message_seconds=0)
                assert guild.me is not None
                embed = action_embed(
                    "Auto-banned", discord.Color.dark_red(), user, guild.me, reason,
                    extra={"Warnings": str(count)},
                )
                await send_modlog(self.bot, guild, embed)
            elif count >= cfg.warn_kick_threshold:
                reason = (
                    f"Auto-kick: reached {count} warnings "
                    f"(threshold {cfg.warn_kick_threshold})"
                )
                try:
                    await user.send(f"You were auto-kicked from **{guild.name}**: {reason}")
                except discord.HTTPException:
                    log.debug("Could not DM auto-kick target", exc_info=True)
                await user.kick(reason=reason[:AUDIT_REASON_MAX])
                assert guild.me is not None
                embed = action_embed(
                    "Auto-kicked", discord.Color.dark_orange(), user, guild.me, reason,
                    extra={"Warnings": str(count)},
                )
                await send_modlog(self.bot, guild, embed)
        except discord.Forbidden:
            log.warning("Auto-escalation forbidden for %s in %s", user.id, guild.id)
        except discord.HTTPException as e:
            log.warning("Auto-escalation HTTP error: %s", e)

    # ---------- /warnings ----------
    @app_commands.command(name="warnings", description="List a user's warnings.")
    @app_commands.describe(user="The user to query.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    async def warnings_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        member = _require_member(interaction)
        if member is None or not member.guild_permissions.kick_members:
            await interaction.response.send_message(
                "❌ You need the **Kick Members** permission.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        warns = await self.db.get_warnings(interaction.guild.id, user.id)
        if not warns:
            await interaction.response.send_message(
                f"{user.mention} has no warnings.", ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Warnings for {user}",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        for w in warns[:WARNINGS_PAGE_SIZE]:
            mod = interaction.guild.get_member(w.moderator_id)
            mod_str = mod.mention if mod else f"`{w.moderator_id}`"
            embed.add_field(
                name=f"#{w.id} • <t:{w.created_at}:R>",
                value=f"By {mod_str}\n{w.reason[:WARN_REASON_MAX]}",
                inline=False,
            )
        if len(warns) > WARNINGS_PAGE_SIZE:
            embed.set_footer(text=f"Showing {WARNINGS_PAGE_SIZE} of {len(warns)} total")
        else:
            embed.set_footer(text=f"{len(warns)} warning(s) total")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- /clearwarnings ----------
    @app_commands.command(
        name="clearwarnings", description="Clear all warnings for a user.",
    )
    @app_commands.describe(user="The user whose warnings will be cleared.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def clearwarnings(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        member = _require_member(interaction)
        if member is None or not member.guild_permissions.ban_members:
            await interaction.response.send_message(
                "❌ You need the **Ban Members** permission.", ephemeral=True,
            )
            return
        assert interaction.guild is not None
        n = await self.db.clear_warnings(interaction.guild.id, user.id)
        embed = action_embed(
            "Warnings Cleared", discord.Color.green(), user, interaction.user,
            f"Cleared {n} warning(s).",
        )
        await interaction.response.send_message(embed=embed)
        await send_modlog(self.bot, interaction.guild, embed)

    # ---------- /delwarn ----------
    @app_commands.command(name="delwarn", description="Delete a single warning by ID.")
    @app_commands.describe(warning_id="The numeric ID of the warning to delete.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    async def delwarn(
        self,
        interaction: discord.Interaction,
        warning_id: int,
    ) -> None:
        member = _require_member(interaction)
        if member is None or not member.guild_permissions.kick_members:
            await interaction.response.send_message(
                "❌ You need the **Kick Members** permission.", ephemeral=True,
            )
            return
        assert interaction.guild is not None
        ok = await self.db.remove_warning(interaction.guild.id, warning_id)
        if ok:
            await interaction.response.send_message(
                f"✅ Removed warning #{warning_id}.", ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"❌ No warning with ID `{warning_id}` in this guild.", ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
