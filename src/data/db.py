"""
Async SQLite layer.
 
Stores per-guild config, moderator warnings, and active tickets.
Single connection — discord.py runs on one event loop, so this is safe.
"""
from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path
from typing import Optional

import aiosqlite

DB_PATH = Path(os.getenv("BOT_DB_PATH", "data/bot.sqlite3"))

@dataclasses.dataclass(slots=True)
class Warning:
    id: int
    guild_id: int
    user_id: int
    moderator_id: int
    reason: str
    created_at: int
    
@dataclasses.dataclass(slots=True)
class GuildConfig:
    guild_id: int
    mod_log_channel_id: Optional[int] = None
    honeypot_channel_id: Optional[int] = None
    staff_role_id: Optional[int] = None
    ticket_category_id: Optional[int] = None
    warn_kick_threshold: int = 3
    warn_ban_threshold: int = 5
    automod_enabled: bool = True

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id            INTEGER PRIMARY KEY,
    mod_log_channel_id  INTEGER,
    honeypot_channel_id INTEGER,
    staff_role_id       INTEGER,
    ticket_category_id  INTEGER,
    warn_kick_threshold INTEGER NOT NULL DEFAULT 3,
    warn_ban_threshold  INTEGER NOT NULL DEFAULT 5,
    automod_enabled     INTEGER NOT NULL DEFAULT 1
);
 
CREATE TABLE IF NOT EXISTS warnings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    moderator_id  INTEGER NOT NULL,
    reason        TEXT    NOT NULL,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings(guild_id, user_id);
 
CREATE TABLE IF NOT EXISTS tickets (
    channel_id   INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'open',
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(guild_id, user_id, status);
"""

class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._conn = aiosqlite.Connection | None = None
        
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        
    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            
    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected — call connect() first"
        return self._conn
    
    async def get_config(self, guild_id: int) -> GuildConfig:
       async with self.conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                cfg = GuildConfig(guild_id=guild_id)
                await self.upsert_config(cfg)
                return cfg
            return GuildConfig(
                guild_id=row["guild_id"],
                mod_log_channel_id=row["mod_log_channel_id"],
                honeypot_channel_id=row["honeypot_channel_id"],
                staff_role_id=row["staff_role_id"],
                ticket_category_id=row["ticket_category_id"],
                warn_kick_threshold=row["warn_kick_threshold"],
                warn_ban_threshold=row["warn_ban_threshold"],
                automod_enabled=bool(row["automod_enabled"]),
        )
        
    async def upsert_config(self, cfg: GuildConfig) -> None:
        await self.conn.execute(
            """
            INSERT INTO guild_config (
                guild_id, mod_log_channel_id, honeypot_channel_id,
                staff_role_id, ticket_category_id,
                warn_kick_threshold, warn_ban_threshold, automod_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                mod_log_channel_id  = excluded.mod_log_channel_id,
                honeypot_channel_id = excluded.honeypot_channel_id,
                staff_role_id       = excluded.staff_role_id,
                ticket_category_id  = excluded.ticket_category_id,
                warn_kick_threshold = excluded.warn_kick_threshold,
                warn_ban_threshold  = excluded.warn_ban_threshold,
                automod_enabled     = excluded.automod_enabled
            """,
            (
                cfg.guild_id, cfg.mod_log_channel_id, cfg.honeypot_channel_id,
                cfg.staff_role_id, cfg.ticket_category_id,
                cfg.warn_kick_threshold, cfg.warn_ban_threshold,
                int(cfg.automod_enabled),
            ),
        )
        await self.conn.commit()
        
    async def add_warning(
        self, guild_id: int, user_id: int, mod_id: int, reason: str,
    ) -> int:
        async with self.conn.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, mod_id, reason, int(time.time())),
        ) as cur:
            wid = cur.lastrowid
        await self.conn.commit()
        return wid or 0
 
    async def get_warnings(self, guild_id: int, user_id: int) -> list[Warning]:
        async with self.conn.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (guild_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
        return [
            Warning(
                id=r["id"],
                guild_id=r["guild_id"],
                user_id=r["user_id"],
                moderator_id=r["moderator_id"],
                reason=r["reason"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
 
    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        async with self.conn.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cur:
            count = cur.rowcount
        await self.conn.commit()
        return count
 
    async def remove_warning(self, guild_id: int, warning_id: int) -> bool:
        async with self.conn.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND id = ?",
            (guild_id, warning_id),
        ) as cur:
            ok = cur.rowcount > 0
        await self.conn.commit()
        return ok
    
    async def get_open_ticket(self, guild_id: int, user_id: int) -> int | None:
        async with self.conn.execute(
            "SELECT channel_id FROM tickets "
            "WHERE guild_id = ? AND user_id = ? AND status = 'open' LIMIT 1",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        return row["channel_id"] if row else None
 
    async def next_ticket_number(self, guild_id: int) -> int:
        async with self.conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 AS next FROM tickets WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
        return row["next"] if row else 1
 
    async def create_ticket(
        self, channel_id: int, guild_id: int, user_id: int, number: int,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO tickets (channel_id, guild_id, user_id, number, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)",
            (channel_id, guild_id, user_id, number, int(time.time())),
        )
        await self.conn.commit()
 
    async def close_ticket(self, channel_id: int) -> None:
        await self.conn.execute(
            "UPDATE tickets SET status = 'closed' WHERE channel_id = ?",
            (channel_id,),
        )
        await self.conn.commit()
 
    async def get_ticket(self, channel_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,),
        ) as cur:
            return await cur.fetchone()