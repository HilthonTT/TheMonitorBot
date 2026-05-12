# Advanced Discord Mod & Ticket Bot

A `discord.py` 2.x bot with rich `/userinfo`, full moderation, automod with
leet-speak-aware language filter, a spam-bot honeypot, and a persistent-button
support-ticket system. State is stored in SQLite via `aiosqlite` (WAL mode).

> **Terminology:** Discord's API and every library call them **guilds**.
> The user-facing UI calls them **servers**. They're the same thing.
> This README and the code use both interchangeably.

## Features

### User info (`src/cogs/user_info.py`)

- `/userinfo` — rich profile embed (account age, join date, roles, status, badges, banner)
- `/avatar` — avatar with PNG / WEBP / GIF download buttons
- "User Info" right-click context menu
- Per-user cooldowns guard the underlying Discord REST calls

### Moderation (`src/cogs/moderation.py`)

- `/kick`, `/ban`, `/unban` with reason + DM-before-action
- `/warn` with DM and persistent record
- `/warnings`, `/clearwarnings`, `/delwarn`
- Hierarchy & self-protection checks (no banning the owner, no banning above the bot's role, etc.)
- Auto-escalation: configurable kick and ban warning thresholds
- All commands defer the interaction before slow REST/DM work, so they never
  time out on the 3-second ack window

### Automod (`src/cogs/automod.py`)

- Bad-language filter with normalization that defeats common bypasses:
  zero-width chars stripped, leet substitutions (`f4ck`, `sh!t`, `b1tch`),
  punctuation/spacing squashed (`f.u.c.k`)
- Whole-word matching via `\b` boundaries to minimize false positives
- Staff (Manage Messages or the configured staff role) are exempt
- Words live in `src/data/bad_words.txt`; `/automod_reload` picks up edits live
- `/automod` toggle per server
- Per-guild config is cached in memory and invalidated on writes, so the
  filter doesn't hit SQLite on every message

### Honeypot (`src/cogs/automod.py`)

- Set any channel as a honeypot via `/set_honeypot`
- Anyone without moderator permissions (Admin, Manage Server, Manage Messages,
  Kick Members, Ban Members) or the configured staff role who posts there is
  banned instantly with 24h of message cleanup
- Detailed report posted to mod log: account age, join date, message preview
- Catches scraping spam bots that post in every channel they can read

### Tickets (`src/cogs/tickets.py`)

- `/ticket_panel` posts an embed with a persistent "Open Ticket" button
- Each ticket creates a private text channel (user + staff role + bot only)
- One open ticket per user per server — enforced via per-user lock **and** a
  UNIQUE partial index in SQLite (defence in depth against double-click races)
- Atomic per-guild ticket numbering — no `MAX()+1` race
- "Close Ticket" button or `/ticket_close` saves a plaintext transcript to the
  mod-log channel and deletes the ticket channel. If the mod log is missing or
  the send fails, the channel is **kept open** and the transcript file is
  posted in-channel so it is never silently lost
- `/ticket_add` and `/ticket_remove` for adding extra participants
- Persistent views — buttons keep working across bot restarts

### Configuration (`src/cogs/admin.py`)

- `/set_modlog` — channel for moderation embeds and transcripts
- `/set_honeypot` / `/clear_honeypot`
- `/ticket_config` — set the ticket category and staff role
- `/set_warn_thresholds` — tune auto-kick / auto-ban
- `/config` — show current settings

---

## Setup

The setup splits into two phases: getting the bot **online** (the developer
portal, environment, and Python side), then getting it **configured** (the
slash commands you run inside your server once the bot is in it).

### Phase 1 — Get the bot online

#### 1. Create the application and bot

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it whatever you want.
2. In the left sidebar, open the **Bot** tab.
3. Under **Privileged Gateway Intents**, enable all three:
   - ✅ **Presence Intent** — needed for `/userinfo` to show online status
   - ✅ **Server Members Intent** — needed for member events and role lists
   - ✅ **Message Content Intent** — needed for automod + honeypot
4. Click **Reset Token** → **Copy**. This is your `DISCORD_TOKEN`. Treat it
   like a password — anyone with it can control the bot.

#### 2. Invite the bot to your server

Still in the developer portal:

1. Open the **OAuth2** tab → **URL Generator**.
2. Under **Scopes**, check `bot` and `applications.commands`.
3. Under **Bot Permissions**, check at minimum:
   `Manage Channels`, `Manage Roles`, `Kick Members`, `Ban Members`,
   `View Channels`, `Send Messages`, `Manage Messages`,
   `Embed Links`, `Attach Files`, `Read Message History`.
4. Copy the generated URL at the bottom, paste it into your browser, pick
   your server, and authorize.

> **Heads up on role hierarchy.** Once the bot joins, drag its auto-created
> role **above** any role it should be able to moderate. Discord forbids
> moderating users whose top role is at or above the bot's. The cog enforces
> this and tells you if you try.

#### 3. Install Python dependencies

Requires **Python 3.10 or newer** (uses PEP 604 unions and `slots=True`).
From the project root:

```bash
python -m venv venv
venv\Scripts\activate           # Windows (PowerShell or cmd)
# source venv/bin/activate      # Linux/macOS
pip install -r requirements.txt
```

#### 4. Configure environment variables

Copy `.env.example` to `.env` and fill in at least the token:

```
DISCORD_TOKEN=paste_the_token_from_step_1_here
DEV_GUILD_ID=                    # optional — see below
ACTIVITY_STATUS="Always monitoring your behavior"  # optional
BOT_DB_PATH=                     # optional — defaults to data/bot.sqlite3
LOG_LEVEL=                       # optional — DEBUG/INFO/WARNING/ERROR
```

##### How to get a `DEV_GUILD_ID` (and why you want one)

`DEV_GUILD_ID` is your Discord **server's** ID. Slash commands sync
**globally by default**, which Discord can take **up to an hour** to
propagate. Setting `DEV_GUILD_ID` makes the bot register commands directly
on that one server, which is **near-instant** — invaluable while iterating.

To copy a server ID:

1. In Discord, go to **User Settings → Advanced** and turn on
   **Developer Mode**.
2. Right-click your server's icon in the sidebar → **Copy Server ID**.
3. Paste it into `.env` as `DEV_GUILD_ID=...` (a long number, no quotes).

The same right-click trick also gives you channel IDs and role IDs if you
ever need them — though the in-server commands below let you pick channels
and roles via Discord's autocomplete UI without touching IDs.

> When you go to production, **clear `DEV_GUILD_ID`** so commands sync
> globally and work in every server the bot is in.

#### 5. Run the bot

```bash
cd src
python bot.py
```

A successful startup looks like:

```
[INFO] data.db: Database ready at data/bot.sqlite3 (schema v1)
[INFO] bot: Loaded extension cogs.admin
[INFO] bot: Loaded extension cogs.automod
[INFO] bot: Loaded extension cogs.moderation
[INFO] bot: Loaded extension cogs.tickets
[INFO] bot: Loaded extension cogs.user_info
[INFO] bot: Synced 22 commands to dev guild 123456789012345678
[INFO] bot: Logged in as YourBot#1234 (id=...)
```

Leave it running. Ctrl+C to stop. On Linux/macOS, SIGTERM is also handled
gracefully so the bot closes its DB connection cleanly under Docker/systemd.

### Phase 2 — Configure the server

With the bot online and in your server, run these slash commands. You need
**Manage Server** for all of them. Discord autocomplete will help — type `/`
and the command name and it'll prompt you for channels/roles.

#### Required for all features

```
/set_modlog channel:#mod-log
```

Pick (or create) a private staff-only channel. All moderation actions,
auto-warn reports, honeypot bans, and ticket transcripts get posted here.
Without this set, those events still happen but transcripts get posted
back in the ticket channel as a fallback.

#### Required for tickets

```
/ticket_config category:Tickets staff_role:@Staff
```

`category` is a Discord **channel category** under which new ticket
channels will be created. `staff_role` is the role that should be granted
access to every ticket. Create both first if you don't have them.

```
/ticket_panel
```

Run this **in the channel where you want users to open tickets from** (e.g.
`#support`). It posts an embed with an "Open Ticket" button. Users click it
to spawn a private channel.

#### Optional — honeypot anti-spam

Create a channel like `#・do-not-post` first. **Important:** in the channel
settings, deny `View Channel` for `@everyone` and for any role that should
never get banned. The point is that no human should ever see or post there;
only spam bots that scrape every readable channel via the API will find it.

```
/set_honeypot channel:#・do-not-post
```

To disable later: `/clear_honeypot`.

#### Optional — warning thresholds

Defaults are auto-kick at 3 warnings, auto-ban at 5. Change with:

```
/set_warn_thresholds kick_threshold:3 ban_threshold:5
```

`ban_threshold` must be ≥ `kick_threshold`.

#### Verify

```
/config
```

Shows everything currently set for this server.

---

## Deployment

### Docker (recommended)

A `Dockerfile` and `docker-compose.yml` are included. The image runs as a
non-root user and persists state to a `/data` volume.

```bash
# Build and start. .env in the repo root is read for credentials.
docker compose up -d --build

# Tail logs
docker compose logs -f bot

# Stop (gracefully closes the gateway and DB)
docker compose down
```

The compose file maps a named volume `bot-data` to `/data` inside the
container; the bot writes its SQLite database to `/data/bot.sqlite3`.
The `restart: unless-stopped` policy auto-restarts on crash.

### Systemd / process manager

For a non-containerised install on Linux, a minimal unit file looks like:

```ini
[Unit]
Description=TheMonitorBot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/srv/themonitorbot/src
EnvironmentFile=/srv/themonitorbot/.env
ExecStart=/srv/themonitorbot/venv/bin/python bot.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
User=bot

[Install]
WantedBy=multi-user.target
```

### Backups

The SQLite database is the only persistent state. Back up `data/bot.sqlite3`
(or the Docker volume) before upgrades. WAL mode means an online
`sqlite3 bot.sqlite3 ".backup '/path/to/backup.sqlite3'"` is safe while the
bot is running.

---

## Project layout

```
TheMonitorBot/
├── src/
│   ├── bot.py                entry point
│   ├── cogs/
│   │   ├── user_info.py      /userinfo, /avatar, context menu
│   │   ├── moderation.py     kick / ban / warn family
│   │   ├── automod.py        bad-language filter + honeypot
│   │   ├── tickets.py        ticket panel + private channels
│   │   └── admin.py          per-server config
│   └── data/
│       ├── db.py             aiosqlite layer (config / warnings / tickets)
│       └── bad_words.txt     filter word list — edit freely
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml            python>=3.10, ruff, mypy, pytest config
├── requirements.txt
├── .env.example
└── README.md
```

---

## Development

The project ships with `pyproject.toml` configuring ruff, mypy, and pytest:

```bash
pip install ruff mypy pytest pytest-asyncio
ruff check src/
mypy src/
pytest
```

---

## Troubleshooting

**"Disallowed intents" / bot won't start.** You didn't enable all three
privileged intents in the developer portal. Go back to **Phase 1, step 1**.

**Slash commands don't appear in Discord.** Either `DEV_GUILD_ID` is wrong
(double-check by right-clicking the server icon → Copy Server ID), or it's
unset and you're waiting on global sync — give it up to an hour, or set
`DEV_GUILD_ID` to your test server for instant updates.

**`Extension 'cogs.X' has no 'setup' function`.** Every cog must end with:

```python
async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MyCog(bot))
```

If you're authoring a new cog, copy that pattern from any existing one.

**Bot can't kick/ban "user with higher role".** Drag the bot's role above
the target's role in **Server Settings → Roles**. The bot's hierarchy
checks are enforcing a real Discord restriction, not being picky.

**Honeypot didn't ban anyone.** Any member with a moderator-tier permission
(Admin, Manage Server, Manage Messages, Kick Members, Ban Members) or the
configured staff role is exempt. Test with an alt account that has no
special permissions.

**Reset everything.** Stop the bot and delete `data/bot.sqlite3` (plus the
`-wal` and `-shm` sidecar files if present). All per-server config,
warnings, and ticket records will be wiped on next start.

---

## Notes

- `/warn` from automod uses the bot as the moderator. Language warnings
  count toward the same auto-escalation thresholds as manual warnings.
- The honeypot exempts every member with mod permissions or the configured
  staff role, so a typo from staff never bans them.
- Persistent ticket buttons survive restarts because their `custom_id`s are
  stable and `bot.add_view()` is called in `setup_hook`.
- SQLite is created on first run at `data/bot.sqlite3` (or `BOT_DB_PATH`
  if set). WAL mode + a 5-second `busy_timeout` are enabled for resilience.
- A `schema_version` table tracks DB upgrades so future migrations stay
  idempotent.
