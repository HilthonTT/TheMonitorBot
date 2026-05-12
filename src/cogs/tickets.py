"""
Support tickets.

Flow:
  1. An admin runs /ticket_config to set the category and staff role.
  2. An admin runs /ticket_panel in the channel where users should open tickets.
     This posts an embed with an "Open Ticket" button.
  3. A user clicks the button → bot creates a private text channel under the
     configured category, visible only to the user, the staff role, and the bot.
  4. Inside the ticket, anyone (opener or staff) can click "Close Ticket". The
     bot saves a plaintext transcript, posts it to the mod log, and deletes
     the channel.

The two button views are PERSISTENT — they have stable custom_ids and
timeout=None, and bot.add_view() is called in setup_hook so they survive
restarts.

Concurrency:
  * A per-(guild,user) asyncio.Lock prevents two simultaneous clicks from
    racing past the "one open ticket per user" check.
  * The DB layer atomically allocates ticket numbers via a per-guild
    counter, and a UNIQUE partial index enforces the invariant at the
    storage layer as a last line of defence.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from data.db import Database

log = logging.getLogger(__name__)

TICKET_OPEN_ID = "ticket:open"
TICKET_CLOSE_ID = "ticket:close"


class TicketPanelView(discord.ui.View):
    """Persistent 'Open Ticket' button posted on a public panel message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open Ticket",
        emoji="🎫",
        style=discord.ButtonStyle.primary,
        custom_id=TICKET_OPEN_ID,
    )
    async def open_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message(
                "❌ Ticket system unavailable.", ephemeral=True,
            )
            return
        await cog._open_ticket(interaction)  # type: ignore[attr-defined]


class TicketCloseView(discord.ui.View):
    """Persistent 'Close Ticket' button placed inside each ticket channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id=TICKET_CLOSE_ID,
    )
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message(
                "❌ Ticket system unavailable.", ephemeral=True,
            )
            return
        await cog._close_ticket(interaction)  # type: ignore[attr-defined]


class Tickets(commands.Cog):
    """Support ticket commands and persistent button handlers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Per-(guild, user) lock to serialise concurrent open attempts.
        self._open_locks: dict[tuple[int, int], asyncio.Lock] = {}

    @property
    def db(self) -> Database:
        return self.bot.db  # type: ignore[attr-defined]

    def _open_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (guild_id, user_id)
        lock = self._open_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._open_locks[key] = lock
        return lock

    # ---------- /ticket_panel ----------
    @app_commands.command(
        name="ticket_panel",
        description="Post an Open-Ticket panel in this channel.",
    )
    @app_commands.describe(
        title="Panel title.",
        description="Panel description shown above the button.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ticket_panel(
        self,
        interaction: discord.Interaction,
        title: str = "Need help?",
        description: str = (
            "Click the button below to open a private support ticket.\n"
            "A staff member will be with you shortly."
        ),
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Abuse of the ticket system may result in moderation action.")
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "❌ Run this in a text channel.", ephemeral=True,
            )
            return
        try:
            await channel.send(embed=embed, view=TicketPanelView())
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"❌ Could not post panel: {e}", ephemeral=True,
            )
            return
        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

    @app_commands.command(
        name="ticket_config",
        description="Configure the ticket system (category & staff role).",
    )
    @app_commands.describe(
        category="Category where ticket channels will be created.",
        staff_role="Role granted access to all tickets.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ticket_config(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        staff_role: discord.Role,
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return

        assert interaction.guild is not None
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.ticket_category_id = category.id
        cfg.staff_role_id = staff_role.id
        await self.db.upsert_config(cfg)
        # Invalidate AutoMod's per-guild config cache.
        automod = self.bot.get_cog("AutoMod")
        if automod is not None and hasattr(automod, "invalidate"):
            automod.invalidate(interaction.guild.id)  # type: ignore[attr-defined]
        await interaction.response.send_message(
            f"✅ Tickets will open under **{category.name}** "
            f"and grant access to {staff_role.mention}.",
            ephemeral=True,
        )

    # ---------- /ticket_open ----------
    @app_commands.command(
        name="ticket_open",
        description="Open a support ticket (alternative to the panel button).",
    )
    @app_commands.guild_only()
    async def ticket_open_slash(self, interaction: discord.Interaction) -> None:
        await self._open_ticket(interaction)

    @app_commands.command(
        name="ticket_close",
        description="Close the current ticket channel.",
    )
    @app_commands.guild_only()
    async def ticket_close_slash(self, interaction: discord.Interaction) -> None:
        await self._close_ticket(interaction)

    @app_commands.command(
        name="ticket_add",
        description="Add a user to the current ticket channel.",
    )
    @app_commands.describe(user="The user to add.")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def ticket_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Run inside a ticket channel.", ephemeral=True,
            )
            return
        ticket = await self.db.get_ticket(channel.id)
        if ticket is None:
            await interaction.response.send_message(
                "❌ This isn't a tracked ticket channel.", ephemeral=True,
            )
            return
        await channel.set_permissions(
            user,
            view_channel=True, send_messages=True,
            read_message_history=True, attach_files=True, embed_links=True,
        )
        await interaction.response.send_message(
            f"✅ Added {user.mention} to the ticket.",
        )

    # ---------- /ticket_remove ----------
    @app_commands.command(
        name="ticket_remove",
        description="Remove a user from the current ticket channel.",
    )
    @app_commands.describe(user="The user to remove.")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def ticket_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Run inside a ticket channel.", ephemeral=True,
            )
            return
        ticket = await self.db.get_ticket(channel.id)
        if ticket is None:
            await interaction.response.send_message(
                "❌ This isn't a tracked ticket channel.", ephemeral=True,
            )
            return
        if user.id == ticket["user_id"]:
            await interaction.response.send_message(
                "❌ Cannot remove the ticket opener.", ephemeral=True,
            )
            return
        await channel.set_permissions(user, overwrite=None)
        await interaction.response.send_message(
            f"✅ Removed {user.mention} from the ticket.",
        )

    # ---------- core helpers ----------
    async def _open_ticket(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "❌ Tickets can only be opened in a server.", ephemeral=True,
            )
            return

        cfg = await self.db.get_config(guild.id)
        if cfg.ticket_category_id is None or cfg.staff_role_id is None:
            await interaction.response.send_message(
                "❌ Tickets aren't configured yet. An admin must run `/ticket_config` first.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(cfg.ticket_category_id)
        staff_role = guild.get_role(cfg.staff_role_id)
        if not isinstance(category, discord.CategoryChannel) or staff_role is None:
            await interaction.response.send_message(
                "❌ Configured category or staff role no longer exists. "
                "Re-run `/ticket_config`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Serialise concurrent open attempts by the same user.
        lock = self._open_lock(guild.id, interaction.user.id)
        async with lock:
            existing = await self.db.get_open_ticket(guild.id, interaction.user.id)
            if existing is not None:
                chan = guild.get_channel(existing)
                if chan is not None:
                    await interaction.followup.send(
                        f"You already have an open ticket: {chan.mention}",
                        ephemeral=True,
                    )
                    return
                # Channel was deleted out-of-band; mark closed and continue.
                await self.db.close_ticket(existing)

            overwrites: dict[
                discord.Role | discord.Member, discord.PermissionOverwrite,
            ] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    attach_files=True, embed_links=True,
                ),
                staff_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_messages=True, attach_files=True, embed_links=True,
                ),
            }
            if guild.me is not None:
                overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True, manage_messages=True, embed_links=True,
                )

            # Allocate a temporary placeholder name; rename once we know the number.
            # We need the channel id before we can persist the ticket row, but the
            # number is allocated atomically by create_ticket in one transaction.
            try:
                channel = await guild.create_text_channel(
                    name="ticket-pending",
                    category=category,
                    overwrites=overwrites,
                    topic=f"Ticket for {interaction.user} ({interaction.user.id})",
                    reason=f"Ticket opened by {interaction.user}",
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ I lack permission to create channels in that category.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                await interaction.followup.send(
                    f"❌ Discord error creating channel: {e}", ephemeral=True,
                )
                return

            try:
                number = await self.db.create_ticket(
                    channel.id, guild.id, interaction.user.id,
                )
            except Exception:
                log.exception("Failed to persist ticket; cleaning up channel %s", channel.id)
                try:
                    await channel.delete(reason="Failed to persist ticket")
                except discord.HTTPException:
                    log.debug("Cleanup delete failed", exc_info=True)
                await interaction.followup.send(
                    "❌ Internal error opening ticket. Please try again.",
                    ephemeral=True,
                )
                return

            try:
                await channel.edit(name=f"ticket-{number:04d}")
            except discord.HTTPException:
                log.debug("Could not rename ticket channel %s", channel.id, exc_info=True)

            welcome = discord.Embed(
                title=f"Ticket #{number:04d}",
                description=(
                    f"Hi {interaction.user.mention}, a member of {staff_role.mention} "
                    f"will be with you shortly.\n\n"
                    f"Please describe your issue in as much detail as possible.\n"
                    f"Click **Close Ticket** below when you're done."
                ),
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            welcome.set_footer(text=f"Opened by {interaction.user}")
            try:
                await channel.send(
                    content=f"{interaction.user.mention} {staff_role.mention}",
                    embed=welcome,
                    view=TicketCloseView(),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=True),
                )
            except discord.HTTPException:
                log.warning("Could not post welcome message in %s", channel.id, exc_info=True)

            await interaction.followup.send(
                f"✅ Your ticket is open: {channel.mention}", ephemeral=True,
            )

    async def _close_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ This isn't a ticket channel.", ephemeral=True,
            )
            return

        ticket = await self.db.get_ticket(channel.id)
        if ticket is None:
            await interaction.response.send_message(
                "❌ This isn't a tracked ticket channel.", ephemeral=True,
            )
            return

        # Owner OR anyone with manage_channels (staff) may close
        is_owner = interaction.user.id == ticket["user_id"]
        is_staff = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_channels
        )
        if not (is_owner or is_staff):
            await interaction.response.send_message(
                "❌ Only the ticket opener or staff can close this.", ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🔒 Closing ticket and saving transcript…",
        )

        # Build plaintext transcript
        opened_iso = datetime.fromtimestamp(
            ticket["created_at"], tz=timezone.utc,
        ).isoformat()
        closed_iso = datetime.now(tz=timezone.utc).isoformat()
        lines = [
            f"# Ticket #{ticket['number']:04d}",
            f"# Opened by user id {ticket['user_id']} on {opened_iso}",
            f"# Closed by {interaction.user} on {closed_iso}",
            "",
        ]
        try:
            async for msg in channel.history(limit=None, oldest_first=True):
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                content = msg.content or ""
                attachments = " ".join(a.url for a in msg.attachments)
                lines.append(f"[{ts}] {msg.author}: {content} {attachments}".rstrip())
        except discord.HTTPException as e:
            lines.append(f"\n(transcript truncated: {e})")

        data = "\n".join(lines).encode("utf-8")

        # Send transcript to mod log if configured. If the send fails, keep
        # the channel alive so the transcript isn't lost forever.
        guild = channel.guild
        cfg = await self.db.get_config(guild.id)
        transcript_sent = False
        if cfg.mod_log_channel_id is not None:
            log_chan = guild.get_channel(cfg.mod_log_channel_id)
            if isinstance(log_chan, discord.TextChannel):
                opener = guild.get_member(ticket["user_id"])
                opener_str = (
                    opener.mention if opener else f"`{ticket['user_id']}`"
                )
                summary = discord.Embed(
                    title=f"Ticket #{ticket['number']:04d} closed",
                    color=discord.Color.dark_grey(),
                    timestamp=discord.utils.utcnow(),
                    description=(
                        f"**Opener:** {opener_str}\n"
                        f"**Closed by:** {interaction.user.mention}\n"
                        f"**Channel:** `{channel.name}`"
                    ),
                )
                try:
                    await log_chan.send(
                        embed=summary,
                        file=discord.File(
                            io.BytesIO(data),
                            filename=f"ticket-{ticket['number']:04d}.txt",
                        ),
                    )
                    transcript_sent = True
                except discord.HTTPException:
                    log.warning(
                        "Failed to post transcript to mod log for channel %s",
                        channel.id, exc_info=True,
                    )

        await self.db.close_ticket(channel.id)

        if not transcript_sent:
            # Either no modlog configured, or send failed. Either way, do not
            # silently drop the transcript — keep the channel for manual
            # review and tell the closer what's up.
            try:
                await channel.send(
                    "⚠️ Transcript could not be sent to the mod log "
                    "(channel kept open for review).",
                    file=discord.File(
                        io.BytesIO(data),
                        filename=f"ticket-{ticket['number']:04d}.txt",
                    ),
                )
            except discord.HTTPException:
                log.debug("Could not attach fallback transcript", exc_info=True)
            return

        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            log.debug("Could not delete ticket channel %s", channel.id, exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
