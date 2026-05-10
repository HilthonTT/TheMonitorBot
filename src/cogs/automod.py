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
 
Staff (Manage Messages permission) are exempt from the language filter.
Admins / Manage Server holders are exempt from honeypot (typo guard).
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
        log.warning(f"{BAD_WORDS_FILE_NAME} missing at {WORDS_FILE} — automod inactive.")
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

class AutoMod(commands.Cog):
    """Bad-language filter and honeypot"""
    
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.words = load_words()
        self.pattern = build_pattern(self.words)
        log.info("AutoMod loaded with %d words", len(self.words))
    
    @property
    def db(self) -> Database:
        return self.bot.db
    
    def _is_bad(self, text: str) -> bool:
        if self.pattern is None:
            return False
        return self.pattern.search(normalize(text)) is not None
 
    async def _modlog(self, guild: discord.Guild, embed: discord.Embed) -> None:
        cfg = await self.db.get_config(guild.id)
        if cfg.mod_log_channel_id is None:
            return
        
        chan = guild.get_channel(cfg.mod_log_channel_id)
        if isinstance(chan, discord.TextChannel):
            try:
                await chan.send(embed=embed)
            except discord.HTTPException:
                pass
            
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        
        cfg = await self.db.get_config(message.guild.id)
 
        # 1) Honeypot first — fastest path to ban a spam bot
        if (
            cfg.honeypot_channel_id is not None
            and message.channel.id == cfg.honeypot_channel_id
        ):
            await self._handle_honeypot(message)
            return
        
        # 2) Bad-language filter
        if not cfg.automod_enabled:
            return
        # Don't auto-warn self
        if message.author.guild_permissions.manage_messages:
            return
        if self._is_bad(message.content):
            await self._handle_bad_language(message, cfg)
            
            
    async def _handle_honeypot(self, message: discord.Message) -> None:
        member = message.author
        assert isinstance(member, discord.Member)
        
        # Final guard: never ban admins / server managers even if they post by mistake
        if (
            member.guild_permissions.administrator
            or member.guild_permissions.manage_guild
        ):
            log.info("Honeypot hit by privileged user %s — ignored.", member.id)
            return
        
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        
        reason = f"Honeypot triggered in #{message.channel.name} (auto spam-bot detection)"
        try:
            await message.guild.ban(  # type: ignore[union-attr]
                member,
                reason=reason,
                delete_message_seconds=86400,  # nuke last 24h of their messages
            )
        except discord.Forbidden:
            log.warning("Honeypot: missing Ban Members permission for %s", member.id)
            return
        except discord.HTTPException as e:
            log.warning("Honeypot: ban failed: %s", e)
            return
        
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
                f"**Message preview:**\n{message.content[:500] or '*(no text)*'}"
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
            pass
 
        reason = f"Inappropriate language: \"{message.content[:200]}\""
        wid = await self.db.add_warning(
            guild.id, member.id, self.bot.user.id, reason,  # type: ignore[union-attr]
        )
        warns = await self.db.get_warnings(guild.id, member.id)
        count = len(warns)
 
        try:
            await member.send(
                f"⚠️ Your message in **{guild.name}** was removed for inappropriate language.\n"
                f"This is warning **#{count}**.",
            )
        except discord.HTTPException:
            pass
 
        try:
            await message.channel.send(
                f"{member.mention}, please watch your language. (warning #{count})",
                delete_after=8,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass
 
        embed = discord.Embed(
            title="🤖 Auto-Warn (language)",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
            description=(
                f"**User:** {member.mention} (`{member.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Warning:** `#{wid}` • **Total:** {count}\n"
                f"**Message:** {message.content[:500]}"
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
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "❌ You need **Manage Server**.", ephemeral=True,
            )
            return
        cfg = await self.db.get_config(interaction.guild.id)
        cfg.automod_enabled = enabled
        await self.db.upsert_config(cfg)
        await interaction.response.send_message(
            f"✅ Automod is now **{'enabled' if enabled else 'disabled'}**.",
            ephemeral=True,
        )
 
    @app_commands.command(
        name="automod_reload",
        description="Reload the bad-words list from disk without restarting.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def automod_reload(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
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
