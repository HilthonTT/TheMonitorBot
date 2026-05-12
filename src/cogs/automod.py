"""
Automod and honeypot.

Two listeners on every guild message:

  1. Honeypot — if the channel is flagged as a honeypot in guild config,
     any non-staff message there triggers an immediate ban + 24h message
     cleanup. Designed to catch self-propagating spam bots that scrape
     channels and post in everything they can see.

  2. Bad-language filter — normalizes the message (lowercase, strip zero-
     width chars, leet substitutions, squash non-letters) and checks against
     a configurable word list. A hit deletes the message, issues a warning
     via the same DB pipeline as /warn, and reuses Moderation.maybe_escalate
     so language-based warnings count toward auto-kick / auto-ban thresholds.

Staff (Manage Messages permission, or the configured staff role) are exempt
from the language filter and from honeypot auto-bans.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from data.db import Database, GuildConfig

log = logging.getLogger(__name__)

BAD_WORDS_FILE_NAME = "bad_words.txt"
WORDS_FILE = Path(__file__).resolve().parent.parent / "data" / BAD_WORDS_FILE_NAME

# --- Tuning constants ---
HONEYPOT_DELETE_HISTORY_SECONDS = 86_400  # 24h
PREVIEW_CHARS = 500
REASON_PREVIEW_CHARS = 200
WARN_NOTICE_DELETE_AFTER = 8
MAX_AUTOMOD_INPUT_LEN = 4_000  # Discord caps at 2000 normally; safety guard

# Common leet-speak / homoglyph substitutions used to bypass filters.
# Applied only when the symbol sits *between* letters, so trailing punctuation
# like "fuck!" isn't corrupted into "fucki" (which breaks word-boundary match).
LEET_LOOKUP = {
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s",
    "7": "t", "@": "a", "$": "s", "!": "i",
}
LEET_INNER_RE = re.compile(
    r"(?<=[a-z])([" + re.escape("".join(LEET_LOOKUP)) + r"])(?=[a-z])"
)
ZW_RE = re.compile(r"[\u200B-\u200D\uFEFF]")  # zero-width chars
LETTERS_ONLY_RE = re.compile(r"[^a-z]+")
SQUASH_INNER_RE = re.compile(r"(?<=[a-z])[^a-z\s](?=[a-z])")
NON_LETTER_RUN_RE = re.compile(r"[^a-z\s]+")


def normalize(text: str) -> str:
    """
    Lowercase + strip zero-width + leet-translate + obfuscation-aware.

    Leet substitutions only fire when the symbol sits between letters
    ('sh!t' -> 'shit'), so trailing punctuation like 'fuck!' is NOT
    rewritten into 'fucki' (which would break the \\b word boundary).
    A single non-letter sandwiched between letters is removed, so
    'f.u.c.k' collapses to 'fuck'. Whitespace is preserved as a word
    boundary; other punctuation runs become a single space. The
    pattern itself ('f+u+c+k+') tolerates letter repetition, so
    'fuuuck' / 'fuckkkk' match without changing the input. Word-boundary
    matching downstream then prevents false positives like 'scunthorpe'.
    """
    t = ZW_RE.sub("", text).lower()
    t = LEET_INNER_RE.sub(lambda m: LEET_LOOKUP[m.group(1)], t)
    t = SQUASH_INNER_RE.sub("", t)
    return NON_LETTER_RUN_RE.sub(" ", t)


def load_words() -> list[str]:
    if not WORDS_FILE.exists():
        log.warning("%s missing at %s — automod inactive.", BAD_WORDS_FILE_NAME, WORDS_FILE)
        return []
    return [
        w.strip().lower()
        for w in WORDS_FILE.read_text(encoding="utf-8").splitlines()
        if w.strip() and not w.lstrip().startswith("#")
    ]


def build_pattern(words: list[str]) -> re.Pattern[str] | None:
    """
    Build an alternation that tolerates letter repetition: each letter is
    quantified with '+', so 'fuck' matches 'fuck', 'fuuuck', and 'fuckkkk'.
    Word-boundary anchors guard against substring false positives
    ('scunthorpe' won't hit 'cunt').
    """
    if not words:
        return None
    parts: list[str] = []
    for w in words:
        cleaned = LETTERS_ONLY_RE.sub("", w.lower())
        if not cleaned:
            continue
        parts.append("".join(f"{re.escape(c)}+" for c in cleaned))
    if not parts:
        return None
    return re.compile(rf"\b(?:{'|'.join(parts)})\b")


def _is_privileged_member(member: discord.Member, staff_role_id: int | None) -> bool:
    """True if the member should be exempt from honeypot bans.

    A wider net than admin/manage_guild: any standard mod permission OR
    the configured staff role counts. Prevents nuking real mods who
    typo into the honeypot.
    """
    perms = member.guild_permissions
    if (
        perms.administrator
        or perms.manage_guild
        or perms.manage_messages
        or perms.kick_members
        or perms.ban_members
    ):
        return True
    if staff_role_id is not None and any(r.id == staff_role_id for r in member.roles):
        return True
    return False


class AutoMod(commands.Cog):
    """Bad-language filter and honeypot"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.words = load_words()
        self.pattern = build_pattern(self.words)
        # Per-guild GuildConfig cache to avoid hitting SQLite on every message.
        # Invalidated explicitly when /automod, /set_modlog, etc. write config.
        self._cfg_cache: dict[int, GuildConfig] = {}
        log.info("AutoMod loaded with %d words", len(self.words))

    @property
    def db(self) -> Database:
        return self.bot.db  # type: ignore[attr-defined]

    async def _get_cfg(self, guild_id: int) -> GuildConfig:
        cached = self._cfg_cache.get(guild_id)
        if cached is not None:
            return cached
        cfg = await self.db.get_config(guild_id)
        self._cfg_cache[guild_id] = cfg
        return cfg

    def invalidate(self, guild_id: int) -> None:
        """Called by other cogs when they upsert guild config."""
        self._cfg_cache.pop(guild_id, None)

    def _is_bad(self, text: str) -> bool:
        if self.pattern is None:
            return False
        if len(text) > MAX_AUTOMOD_INPUT_LEN:
            text = text[:MAX_AUTOMOD_INPUT_LEN]
        return self.pattern.search(normalize(text)) is not None

    async def _modlog(self, guild: discord.Guild, embed: discord.Embed) -> None:
        cfg = await self._get_cfg(guild.id)
        if cfg.mod_log_channel_id is None:
            return

        chan = guild.get_channel(cfg.mod_log_channel_id)
        if isinstance(chan, discord.TextChannel):
            try:
                await chan.send(embed=embed)
            except discord.HTTPException:
                log.debug("Failed to send modlog to %s", chan.id, exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        cfg = await self._get_cfg(message.guild.id)

        # 1) Honeypot first — fastest path to ban a spam bot
        if (
            cfg.honeypot_channel_id is not None
            and message.channel.id == cfg.honeypot_channel_id
        ):
            await self._handle_honeypot(message, cfg)
            return

        # 2) Bad-language filter
        if not cfg.automod_enabled:
            return
        # Don't auto-warn moderators
        if message.author.guild_permissions.manage_messages:
            return
        if cfg.staff_role_id is not None and any(
            r.id == cfg.staff_role_id for r in message.author.roles
        ):
            return
        if self._is_bad(message.content):
            await self._handle_bad_language(message, cfg)

    async def _handle_honeypot(
        self, message: discord.Message, cfg: GuildConfig,
    ) -> None:
        member = message.author
        assert isinstance(member, discord.Member)
        assert message.guild is not None

        # Wider safety net: never ban any moderator, even by mistake.
        if _is_privileged_member(member, cfg.staff_role_id):
            log.info("Honeypot hit by privileged user %s — ignored.", member.id)
            return

        try:
            await message.delete()
        except discord.HTTPException:
            log.debug("Honeypot: could not delete message", exc_info=True)

        # Channel name may not exist on every channel type — guard.
        chan_name = getattr(message.channel, "name", "?")
        reason = (
            f"Honeypot triggered in #{chan_name} (auto spam-bot detection)"
        )[:512]  # Discord audit-log reason cap
        try:
            await message.guild.ban(
                member,
                reason=reason,
                delete_message_seconds=HONEYPOT_DELETE_HISTORY_SECONDS,
            )
        except discord.Forbidden:
            log.warning("Honeypot: missing Ban Members permission for %s", member.id)
            return
        except discord.HTTPException as e:
            log.warning("Honeypot: ban failed: %s", e)
            return

        preview = discord.utils.escape_markdown(message.content[:PREVIEW_CHARS] or "")
        embed = discord.Embed(
            title="🍯 Honeypot Auto-Ban",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
            description=(
                f"**User:** {member.mention} `{member}` (`{member.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Account age:** <t:{int(member.created_at.timestamp())}:R>\n"
                f"**Joined server:** "
                f"{f'<t:{int(member.joined_at.timestamp())}:R>' if member.joined_at else '—'}\n"
                f"**Message preview:**\n{preview or '*(no text)*'}"
            ),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await self._modlog(message.guild, embed)

    async def _handle_bad_language(
        self,
        message: discord.Message,
        cfg: GuildConfig,
    ) -> None:
        member = message.author
        assert isinstance(member, discord.Member)
        guild = message.guild
        assert guild is not None

        try:
            await message.delete()
        except discord.HTTPException:
            log.debug("AutoMod: could not delete message", exc_info=True)

        preview_for_reason = message.content[:REASON_PREVIEW_CHARS]
        reason = f'Inappropriate language: "{preview_for_reason}"'[:512]
        bot_id = self.bot.user.id if self.bot.user else 0
        wid = await self.db.add_warning(guild.id, member.id, bot_id, reason)
        warns = await self.db.get_warnings(guild.id, member.id)
        count = len(warns)

        try:
            await member.send(
                f"⚠️ Your message in **{guild.name}** was removed for inappropriate language.\n"
                f"This is warning **#{count}**.",
            )
        except discord.HTTPException:
            log.debug("AutoMod: could not DM warned user", exc_info=True)

        try:
            await message.channel.send(  # type: ignore[union-attr]
                f"{member.mention}, please watch your language. (warning #{count})",
                delete_after=WARN_NOTICE_DELETE_AFTER,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            log.debug("AutoMod: could not post warn notice", exc_info=True)

        preview = discord.utils.escape_markdown(message.content[:PREVIEW_CHARS])
        embed = discord.Embed(
            title="🤖 Auto-Warn (language)",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
            description=(
                f"**User:** {member.mention} (`{member.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Warning:** `#{wid}` • **Total:** {count}\n"
                f"**Message:** {preview}"
            ),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await self._modlog(guild, embed)

        # Hand off to Moderation cog for threshold escalation
        mod_cog = self.bot.get_cog("Moderation")
        if mod_cog is not None and hasattr(mod_cog, "maybe_escalate"):
            await mod_cog.maybe_escalate(guild, member, count, cfg)  # type: ignore[attr-defined]

    # ---------- admin commands ----------
    @app_commands.command(
        name="automod", description="Toggle the bad-language auto-filter.",
    )
    @app_commands.describe(enabled="Whether to enable automod for this server.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def automod_toggle(
        self, interaction: discord.Interaction, enabled: bool,
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return
        assert interaction.guild is not None
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.automod_enabled = enabled
        await self.db.upsert_config(cfg)
        self.invalidate(interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Automod is now **{'enabled' if enabled else 'disabled'}**.",
            ephemeral=True,
        )

    @app_commands.command(
        name="automod_reload",
        description="Reload the bad-words list from disk without restarting.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def automod_reload(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return
        self.words = load_words()
        self.pattern = build_pattern(self.words)
        await interaction.response.send_message(
            f"✅ Reloaded {len(self.words)} word(s).", ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoMod(bot))
