"""
Async SQLite layer.

Stores per-guild config, moderator warnings, and active tickets.
Single connection — discord.py runs on one event loop, so this is safe.
WAL mode + busy_timeout are enabled for resilience under burst load.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiosqlite

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/bot.sqlite3"

# Bump when SCHEMA changes; apply_migrations handles the upgrade path.
SCHEMA_VERSION = 1


@dataclasses.dataclass(slots=True)
class WarningRecord:
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
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

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

-- One open ticket per (guild, user). Closed tickets are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_open
    ON tickets(guild_id, user_id) WHERE status = 'open';

-- Per-guild monotonic ticket counter. Avoids the MAX()+1 race.
CREATE TABLE IF NOT EXISTS ticket_counter (
    guild_id INTEGER PRIMARY KEY,
    last_num INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Optional[Path] = None) -> None:
        # Resolve lazily so env vars loaded after import still take effect.
        self.path: Path = path or Path(os.getenv("BOT_DB_PATH", DEFAULT_DB_PATH))
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row

        # Reliability pragmas: WAL + busy_timeout + FK enforcement.
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute("PRAGMA busy_timeout=5000;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")

        await self._conn.executescript(SCHEMA)
        await self._apply_migrations()
        await self._conn.commit()
        log.info("Database ready at %s (schema v%d)", self.path, SCHEMA_VERSION)

    async def _apply_migrations(self) -> None:
        """Idempotent schema upgrades. SCHEMA itself is CREATE-IF-NOT-EXISTS."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT version FROM schema_version LIMIT 1",
        ) as cur:
            row = await cur.fetchone()
        current = row["version"] if row else 0

        # Future migrations go here, gated by `if current < N: ...`.
        if current != SCHEMA_VERSION:
            await self._conn.execute("DELETE FROM schema_version;")
            await self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected — call connect() first"
        return self._conn

    # ---------- guild_config ----------

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

    # ---------- warnings ----------

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

    async def get_warnings(self, guild_id: int, user_id: int) -> list[WarningRecord]:
        async with self.conn.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (guild_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
        return [
            WarningRecord(
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

    # ---------- tickets ----------

    async def get_open_ticket(self, guild_id: int, user_id: int) -> int | None:
        async with self.conn.execute(
            "SELECT channel_id FROM tickets "
            "WHERE guild_id = ? AND user_id = ? AND status = 'open' LIMIT 1",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        return row["channel_id"] if row else None

    async def create_ticket(
        self, channel_id: int, guild_id: int, user_id: int,
    ) -> int:
        """Atomically allocate the next ticket number and insert the row.

        The per-guild counter avoids the MAX(number)+1 race; the UNIQUE
        partial index on (guild_id, user_id) WHERE status='open' protects
        against double-open from concurrent button clicks.
        Returns the allocated ticket number.
        """
        try:
            await self.conn.execute("BEGIN IMMEDIATE;")
            await self.conn.execute(
                "INSERT INTO ticket_counter (guild_id, last_num) VALUES (?, 0) "
                "ON CONFLICT(guild_id) DO NOTHING",
                (guild_id,),
            )
            await self.conn.execute(
                "UPDATE ticket_counter SET last_num = last_num + 1 "
                "WHERE guild_id = ?",
                (guild_id,),
            )
            async with self.conn.execute(
                "SELECT last_num FROM ticket_counter WHERE guild_id = ?",
                (guild_id,),
            ) as cur:
                row = await cur.fetchone()
            number = int(row["last_num"]) if row else 1

            await self.conn.execute(
                "INSERT INTO tickets "
                "(channel_id, guild_id, user_id, number, status, created_at) "
                "VALUES (?, ?, ?, ?, 'open', ?)",
                (channel_id, guild_id, user_id, number, int(time.time())),
            )
            await self.conn.commit()
            return number
        except Exception:
            await self.conn.rollback()
            raise

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
