# The Monitor - Advanced Discord Mod & Ticket Bot

A `discord.py` 2.x bot with rich `/userinfo`, full moderation, automod with
leet-speak-aware language filter, a spam-bot honeypot, and a persistent-button
support-ticket system. State is stored in SQLite via `aiosqlite`.

## Features

### User info (`cogs/userinfo.py`)

- `/userinfo` — rich profile embed (account age, join date, roles, status, badges, banner)
- `/avatar` — avatar with PNG / WEBP / GIF download buttons
- "User Info" right-click context menu

### Moderation (`cogs/moderation.py`)

- `/kick`, `/ban`, `/unban` with reason + DM-before-action
- `/warn` with DM and persistent record
- `/warnings`, `/clearwarnings`, `/delwarn`
- Hierarchy & self-protection checks (no banning the owner, no banning above the bot's role, etc.)
- Auto-escalation: configurable `kick_at` and `ban_at` warning thresholds

### Automod (`cogs/automod.py`)

- Bad-language filter with normalization that defeats common bypasses:
  zero-width chars stripped, leet substitutions (`f4ck`, `sh!t`, `b1tch`),
  punctuation/spacing squashed (`f.u.c.k`)
- Whole-word matching via `\b` boundaries to minimize false positives
- Staff (Manage Messages) are exempt
- Words live in `data/bad_words.txt`; `/automod_reload` picks up edits live
- `/automod` toggle per guild

### Honeypot (`cogs/automod.py`)

- Set any channel as a honeypot via `/set_honeypot`
- Anyone (other than admins / Manage Server holders) who posts there is
  banned instantly with 24h of message cleanup
- Detailed report posted to mod log: account age, join date, message preview
- Catches scraping spam bots that post in every channel they can read

### Tickets (`cogs/tickets.py`)

- `/ticket_panel` posts an embed with a persistent "Open Ticket" button
- Each ticket creates a private text channel (user + staff role + bot only)
- One open ticket per user per guild (enforced)
- "Close Ticket" button or `/ticket_close` saves a plaintext transcript to the
  mod-log channel and deletes the ticket channel
- `/ticket_add` and `/ticket_remove` for adding extra participants
- Persistent views — buttons keep working across bot restarts

### Configuration (`cogs/admin.py`)

- `/set_modlog` — channel for moderation embeds and transcripts
- `/set_honeypot` / `/clear_honeypot`
- `/ticket_config` — set the ticket category and staff role
- `/set_warn_thresholds` — tune auto-kick / auto-ban
- `/config` — show current settings

## Setup

### 1. Discord developer portal

Create an application + bot at <https://discord.com/developers/applications>
and **enable these privileged intents** on the Bot tab:

- Server Members Intent
- Presence Intent
- **Message Content Intent** — required for automod and honeypot

When inviting the bot, grant at minimum:
`Manage Channels`, `Manage Roles`, `Kick Members`, `Ban Members`,
`Read Messages/View Channels`, `Send Messages`, `Manage Messages`,
`Embed Links`, `Attach Files`, `Read Message History`.

### 2. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

### 3. Configure environment

Copy `.env.example` to `.env` and fill in:

```
DISCORD_TOKEN=your_bot_token
DEV_GUILD_ID=123456789012345678   # optional — instant slash sync to a single guild
BOT_DB_PATH=data/bot.sqlite3      # optional — override DB location
```

### 4. Run

```bash
python bot.py
```

### 5. Configure per-guild (run once after the bot joins)

In your server, run:

```
/set_modlog       channel:#mod-log
/ticket_config    category:Tickets staff_role:@Staff
/ticket_panel     in the channel where users should open tickets
```

Optional:

```
/set_honeypot         channel:#trap        (deny @everyone view first!)
/set_warn_thresholds  kick_at:3 ban_at:5
```

## Project layout

```
discord_bot/
├── bot.py                    entry point
├── cogs/
│   ├── userinfo.py           /userinfo, /avatar, context menu
│   ├── moderation.py         kick / ban / warn family
│   ├── automod.py            bad-language filter + honeypot
│   ├── tickets.py            ticket panel + private channels
│   └── admin.py              per-guild config
├── data/
│   ├── db.py                 aiosqlite layer (config / warnings / tickets)
│   └── bad_words.txt         filter word list — edit freely
├── requirements.txt
├── .env.example
└── README.md
```

## Notes

- `/warn` from automod uses the bot as the moderator. Language warnings count
  toward the same auto-escalation thresholds as manual warnings.
- The honeypot exempts admins and Manage Server holders so a typo from staff
  never bans them.
- Persistent ticket buttons survive restarts because their `custom_id`s are
  stable and `bot.add_view()` is called in `setup_hook`.
- SQLite is created on first run at `data/bot.sqlite3`. Delete the file to
  reset all guild state.
