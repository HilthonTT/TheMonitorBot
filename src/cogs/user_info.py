"""
User-info commands.
 
Exposes:
  /userinfo [user] [ephemeral]   — full profile embed
  /avatar  [user]                — avatar showcase with format download buttons
  Context menu "User Info"       — right-click on a user → Apps → User Info
"""
from __future__ import annotations

import logging
from typing import Optional
 
import discord
from discord import app_commands
from discord.ext import commands
 
log = logging.getLogger(__name__)

# Map PublicUserFlags attribute names -> (label, emoji)
FLAG_LABELS: dict[str, tuple[str, str]] = {
    "staff": ("Discord Staff", "🛡️"),
    "partner": ("Partnered Server Owner", "🤝"),
    "hypesquad": ("HypeSquad Events", "🎉"),
    "bug_hunter": ("Bug Hunter (Lvl 1)", "🐛"),
    "bug_hunter_level_2": ("Bug Hunter (Lvl 2)", "🐞"),
    "hypesquad_bravery": ("HypeSquad Bravery", "💜"),
    "hypesquad_brilliance": ("HypeSquad Brilliance", "💗"),
    "hypesquad_balance": ("HypeSquad Balance", "💚"),
    "early_supporter": ("Early Supporter", "🥇"),
    "verified_bot_developer": ("Early Verified Bot Dev", "🤖"),
    "discord_certified_moderator": ("Certified Moderator", "🎓"),
    "active_developer": ("Active Developer", "🛠️"),
}

STATUS_EMOJI: dict[discord.Status, str] = {
    discord.Status.online: "🟢",
    discord.Status.idle: "🌙",
    discord.Status.dnd: "⛔",
    discord.Status.offline: "⚫",
    discord.Status.invisible: "⚫",
}

def humanize_flags(user: discord.User | discord.Member) -> str:
    flags = user.public_flags
    out = [
        f"{emoji} {label}"
        for attr, (label, emoji) in FLAG_LABELS.items()
        if getattr(flags, attr, False)
    ]
    return "\n".join(out) if out else "—"

def format_activity(activity: discord.BaseActivity | None) -> str:
    if activity is None:
        return "—"
    if isinstance(activity, discord.Spotify):
        return f"🎵 Listening to **{activity.title}** by {', '.join(activity.artists)}"
    if isinstance(activity, discord.Game):
        return f"🎮 Playing **{activity.name}**"
    if isinstance(activity, discord.Streaming):
        return f"📺 Streaming **{activity.name}**"
    if isinstance(activity, discord.CustomActivity):
        emoji = f"{activity.emoji} " if activity.emoji else ""
        text = (activity.name or "").strip()
        return f"💭 {emoji}{text}".strip() or "—"
    if isinstance(activity, discord.Activity):
        return f"📝 {activity.name}"
    return str(activity)

async def build_user_embed(
    bot: commands.Bot,
    target: discord.User | discord.Member,
) -> tuple[discord.Embed, discord.User]:
    """Build a rich embed for a User or Member and return it alongside the
    fully-fetched User object (which carries banner / accent_color)."""
    full_user = await bot.fetch_user(target.id)
    
    # Prefer top-role color in a guild, then accent color, then blurple.
    if isinstance(target, discord.Member) and target.color != discord.Color.default():
        color = target.color
    elif full_user.accent_color is not None:
        color = full_user.accent_color
    else:
        color = discord.Color.blurple()
        
    embed = discord.Embed(
        title=str(target),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    if full_user.banner is not None:
        embed.set_image(url=full_user.banner.url)
        
    # --- Identity ---
    embed.add_field(name="ID", value=f"`{target.id}`", inline=True)
    embed.add_field(name="Username", value=f"@{target.name}", inline=True)
    if target.global_name and target.global_name != target.name:
        embed.add_field(name="Display Name", value=target.global_name, inline=True)
        
    # --- Account creation ---
    created_ts = int(target.created_at.timestamp())
    embed.add_field(
        name="Account Created",
        value=f"<t:{created_ts}:F>\n(<t:{created_ts}:R>)",
        inline=True,
    )
    
    # --- Member-specific fields ---
    if isinstance(target, discord.Member):
        if target.joined_at is not None:
            joined_ts = int(target.joined_at.timestamp())
            embed.add_field(
                name="Joined Server",
                value=f"<t:{joined_ts}:F>\n(<t:{joined_ts}:R>)",
                inline=True,
            )
        if target.premium_since is not None:
            boost_ts = int(target.premium_since.timestamp())
            embed.add_field(
                name="Boosting Since",
                value=f"<t:{boost_ts}:R>",
                inline=True,
            )
 
        roles = [r for r in reversed(target.roles) if r.name != "@everyone"]
        if roles:
            roles_str = " ".join(r.mention for r in roles[:20])
            if len(roles) > 20:
                roles_str += f" *(+{len(roles) - 20} more)*"
            embed.add_field(
                name=f"Roles ({len(roles)})",
                value=roles_str,
                inline=False,
            )
 
        embed.add_field(
            name="Status",
            value=f"{STATUS_EMOJI.get(target.status, '⚫')} {target.status.name.title()}",
            inline=True,
        )
        primary_activity = next(
            (a for a in target.activities if not isinstance(a, discord.CustomActivity)),
            target.activity,
        )
        embed.add_field(name="Activity", value=format_activity(primary_activity), inline=True)
 
        if target.guild_permissions.administrator:
            embed.add_field(name="Key Permission", value="👑 Administrator", inline=True)
 
    # --- Account flags ---
    tags: list[str] = []
    if target.bot:
        tags.append("🤖 Bot")
    if target.system:
        tags.append("⚙️ System")
    if tags:
        embed.add_field(name="Account Type", value=" ".join(tags), inline=True)
 
    embed.add_field(name="Badges", value=humanize_flags(target), inline=False)
    embed.set_footer(text="UserInfoBot")
    return embed, full_user

def build_asset_view(target: discord.User | discord.Member, full_user: discord.User) -> discord.ui.View:
    """View with link buttons to the raw avatar / server avatar / banner."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Avatar",
        style=discord.ButtonStyle.link,
        url=target.display_avatar.url,
    ))
    if isinstance(target, discord.Member) and target.guild_avatar is not None:
        view.add_item(discord.ui.Button(
            label="Server Avatar",
            style=discord.ButtonStyle.link,
            url=target.guild_avatar.url,
        ))
    if full_user.banner is not None:
        view.add_item(discord.ui.Button(
            label="Banner",
            style=discord.ButtonStyle.link,
            url=full_user.banner.url,
        ))
    return view

class UserInfo(commands.Cog):
    """User information commands."""
    
    def __init__(self, bot: commands.bot) -> None:
        self.bot = bot
        # Context menus must be registered as standalone app_commands; the
        # decorator-style on cog methods only works with bot.tree.context_menu.
        self.ctx_menu = app_commands.ContextMenu(
            name="User Info",
            callback=self._user_info_context,
        )
        self.bot.tree.add_command(self.ctx_menu)
        
    async def cog_unload(self) -> None: # graceful reload support
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
        
    async def _resolve_target(
        self,
        interaction: discord.Interaction,
        query: str | None,
    ) -> discord.User | discord.Member | None:
        """Resolve mention / numeric ID / username to a User or Member.
 
        Order:
          1. None                  -> the invoker
          2. <@123> mention        -> strip & treat as ID
          3. all-digit string      -> fetch by ID (member if in guild, else user)
          4. plain name (in guild) -> match name / global_name / nickname
        """
        if query is None:
            return interaction.user
        
        query = query.strip()
        
        # Mention form <@id> or <@!id>
        if query.startswith("<@") and query.endswith(">"):
            query = query.strip("<@!>")
            
        # Numeric ID
        if query.isdigit():
            uid = int(query)
            if interaction.guild is not None:
                member = interaction.guild.get_member(uid)
                if member is not None:
                    return member
            try:
                return await self.bot.fetch_user(uid)
            except discord.NotFound:
                return None
            except discord.HTTPException as exc:
                log.warning("fetch_user failed for %s: %s", uid, exc)
                return None
            
        # Username search — only meaningful inside a guild
        if interaction.guild is not None:
            lowered = query.lower().lstrip("@")
            for m in interaction.guild.members:
                if (
                    m.name.lower() == lowered
                    or (m.global_name and m.global_name.lower() == lowered)
                    or (m.nick and m.nick.lower() == lowered)
                ):
                    return m
 
        return None

    # ----- /userinfo -----
 
    @app_commands.command(
        name="userinfo",
        description="Show detailed info about a user (mention, ID, or username).",
    )
    @app_commands.describe(
        user="A mention, user ID, or username. Defaults to yourself.",
        ephemeral="If true, only you will see the response.",
    )
    async def userinfo(
        self,
        interaction: discord.Interaction,
        user: Optional[str] = None,
        ephemeral: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        
        target = await self._resolve_target(interaction, user)
        if target is None:
            await interaction.followup.send(
                f"❌ Could not find a user matching `{user}`.",
                ephemeral=True,
            )
            return
        
        try:
            embed, full_user = await build_user_embed(self.bot, target)
            view = build_asset_view(target, full_user)
            await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)
        except discord.HTTPException as exc:
            log.exception("Failed building user info")
            await interaction.followup.send(
                f"⚠️ Discord API error: {exc}",
                ephemeral=True,
            )
            
    # ----- /avatar -----
    @app_commands.command(
        name="avatar",
        description="Show a user's avatar with download links for each format.",
    )
    @app_commands.describe(user="A mention, user ID, or username. Defaults to yourself.")
    async def avatar(
        self,
        interaction: discord.Interaction,
        user: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        target = await self._resolve_target(interaction, user)
        if target is None:
            await interaction.followup.send(
                f"❌ Could not find a user matching `{user}`.",
                ephemeral=True,
            )
            return
 
        embed = discord.Embed(
            title=f"{target}'s avatar",
            color=discord.Color.blurple(),
        )
        embed.set_image(url=target.display_avatar.url)
        
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="PNG",
            style=discord.ButtonStyle.link,
            url=target.display_avatar.replace(format="png", size=4096).url,
        ))
        view.add_item(discord.ui.Button(
            label="WEBP",
            style=discord.ButtonStyle.link,
            url=target.display_avatar.replace(format="webp", size=4096).url,
        ))
        if target.display_avatar.is_animated():
            view.add_item(discord.ui.Button(
                label="GIF",
                style=discord.ButtonStyle.link,
                url=target.display_avatar.replace(format="gif", size=4096).url,
            ))
 
        await interaction.followup.send(embed=embed, view=view)
        
    # ----- context menu callback -----
 
    async def _user_info_context(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, full_user = await build_user_embed(self.bot, member)
        view = build_asset_view(member, full_user)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
 
 
async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UserInfo(bot))
    