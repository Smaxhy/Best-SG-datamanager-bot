# Discord Data Manager Bot

A Discord bot that scans channels for player activity logs and automatically updates a Google Spreadsheet with EP/TP/MP stat changes. Built for Roblox/AOT-style communities where Data Managers record player activity.

---

## What It Does

1. A Data Manager runs `/scan channel:#rook-logs`
2. The bot scans the last 48 hours of messages
3. It finds valid player logs, extracts the **in-game Username**, EP, TP, MP
4. It looks up the player in Google Sheets (across Rook / Bastion / Ancilliary tabs)
5. It adds the stat changes to their existing totals
6. It tracks which messages have been processed (no duplicates ever)
7. It credits the Data Manager with a weekly score
8. It posts a detailed summary embed

---

## Project Structure

```
discord-data-manager/
├── bot.py                    # Entry point
├── config.py                 # All settings from env vars
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── commands/
│   ├── scan.py               # /scan slash command
│   └── stats.py              # /dmstats slash command
├── services/
│   ├── database_service.py   # SQLite/Postgres — duplicates, DM stats
│   ├── sheets_service.py     # Google Sheets read/write
│   ├── discord_service.py    # Core scan orchestration
│   └── stats_service.py      # DM stats queries
├── parser/
│   └── log_parser.py         # Isolated log format parser
├── models/
│   ├── base.py
│   ├── processed_log.py
│   ├── data_manager.py
│   ├── scan_history.py
│   └── player.py             # Dataclasses (not DB)
└── tests/
    ├── test_parser.py
    └── test_database.py
```

---

## Setup Guide

### 1. Create the Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it (e.g. "Data Manager Bot") → Create
3. Go to **Bot** in the left sidebar
4. Click **Reset Token** → copy the token → save it securely (this is your `DISCORD_TOKEN`)
5. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent** ✅ (required to read log messages)
   - **Server Members Intent** ✅ (required to check roles)
6. Go to **OAuth2** → **URL Generator**
7. Check these scopes: `bot`, `applications.commands`
8. Check these bot permissions:
   - Read Messages/View Channels
   - Read Message History
   - Send Messages
   - Embed Links
   - Use Slash Commands
9. Copy the generated URL → open it in your browser → select your server → Authorize

### 2. Get Role IDs

1. In Discord, go to **Settings** → **Advanced** → enable **Developer Mode**
2. Go to your server → **Server Settings** → **Roles**
3. Right-click the **Data Manager** role → **Copy Role ID** → save as `DATA_MANAGER_ROLE_ID`
4. Right-click the **Staff** role → **Copy Role ID** → save as `STAFF_ROLE_ID`

### 3. Set Up Google Sheets API

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Go to **APIs & Services** → **Library**
4. Search for **Google Sheets API** → Enable it
5. Search for **Google Drive API** → Enable it
6. Go to **APIs & Services** → **Credentials**
7. Click **Create Credentials** → **Service Account**
8. Name it (e.g. "data-manager-bot") → Create and Continue → Done
9. Click the service account → **Keys** tab → **Add Key** → **Create new key** → JSON → Download
10. Rename the file to `service_account.json`

### 4. Share Your Spreadsheet

1. Open the downloaded JSON file → find the `client_email` field (looks like `something@project.iam.gserviceaccount.com`)
2. Open your Google Spreadsheet
3. Click **Share** → paste that email → give **Editor** access → Share
4. Copy your spreadsheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit`

### 5. Spreadsheet Format

Your spreadsheet must have three tabs named exactly: **Rook**, **Bastion**, **Ancilliary**

Each tab needs headers in row 1 (column order doesn't matter):

| Username | Lore Name | Rank | EP | TP | MP |
|----------|-----------|------|----|----|----|
| Smaxhy   | Jessica Cruz | PFC | 10 | 5 | 2 |

The bot finds columns by header name, so you can add other columns freely.

### 6. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your values:
- `DISCORD_TOKEN` — from step 1
- `DATA_MANAGER_ROLE_ID` — from step 2
- `STAFF_ROLE_ID` — from step 2
- `GOOGLE_SPREADSHEET_ID` — from step 4

### 7. Run Locally (for testing)

```bash
python -m pip install -r requirements.txt
python bot.py
```

For instant slash command registration during dev, set `DEV_GUILD_ID` to your server ID.

---

## Cloud Deployment (24/7)

### Option A: Railway (recommended — easiest)

1. Push your code to GitHub (make sure `.env` and `service_account.json` are in `.gitignore`)
2. Go to [railway.app](https://railway.app/) → **New Project** → **Deploy from GitHub**
3. Select your repo
4. Go to **Variables** → add each variable from `.env.example`
5. For the service account, use `GOOGLE_SERVICE_ACCOUNT_JSON` and paste the **entire JSON contents** of `service_account.json`
6. Railway auto-detects the Dockerfile and deploys
7. The bot restarts automatically on crashes

### Option B: Render

1. Go to [render.com](https://render.com/) → **New** → **Background Worker**
2. Connect your GitHub repo
3. Set **Docker** as the environment
4. Add environment variables
5. Deploy

### Option C: VPS with Docker

```bash
# On your VPS:
git clone <your-repo>
cd discord-data-manager

# Create .env file with your values
cp .env.example .env
nano .env

# Place service_account.json in the project root
# Then:
docker compose up -d --build

# Check logs:
docker compose logs -f

# Restart:
docker compose restart

# Update (after git pull):
docker compose up -d --build
```

---

## Commands

### `/scan channel:#channel-name`
Scans the channel for player logs from the last 48 hours. Requires **Data Manager** or **Staff** role.

### `/dmstats`
Shows your Data Manager statistics for the current week and history.

### `/dmstats user:@someone`
View another user's stats (Staff only).

---

## Configuration Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_TOKEN` | ✅ | — | Bot token from Discord Developer Portal |
| `GOOGLE_SPREADSHEET_ID` | ✅ | — | ID from the spreadsheet URL |
| `DATA_MANAGER_ROLE_ID` | ✅ | — | Discord role ID |
| `STAFF_ROLE_ID` | ✅ | — | Discord role ID |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | ✅* | `service_account.json` | Path to service account JSON |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅* | — | Full JSON string (alternative to file) |
| `ROOK_SHEET_NAME` | — | `Rook` | Tab name in spreadsheet |
| `BASTION_SHEET_NAME` | — | `Bastion` | Tab name in spreadsheet |
| `ANCILLIARY_SHEET_NAME` | — | `Ancilliary` | Tab name in spreadsheet |
| `SCAN_HOURS` | — | `48` | How far back to scan |
| `DATABASE_URL` | — | `sqlite+aiosqlite:///./data/bot.db` | Database connection string |
| `ALLOWED_CHANNEL_IDS` | — | *(empty = allow all)* | Comma-separated channel IDs |
| `DEV_GUILD_ID` | — | — | For instant slash command sync during dev |

*One of `GOOGLE_SERVICE_ACCOUNT_FILE` or `GOOGLE_SERVICE_ACCOUNT_JSON` is required.

---

## Changing Things

**Change scan period:** Edit `SCAN_HOURS` in `.env` → restart bot.

**Change sheet names:** Edit `ROOK_SHEET_NAME` / `BASTION_SHEET_NAME` / `ANCILLIARY_SHEET_NAME` in `.env` → restart.

**Change role IDs:** Edit `DATA_MANAGER_ROLE_ID` / `STAFF_ROLE_ID` in `.env` → restart.

**Restrict channels:** Set `ALLOWED_CHANNEL_IDS=123,456,789` in `.env` → restart.

**Change log format:** Edit `parser/log_parser.py`. The regex patterns at the top control what gets extracted. The rest of the bot doesn't need to change.

**Switch to PostgreSQL:** Set `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname` in `.env`, install `asyncpg` (`pip install asyncpg`), restart. Tables auto-create.

---

## Troubleshooting

**Bot is online but slash commands don't appear:**
- Global sync takes up to 1 hour. Set `DEV_GUILD_ID` for instant sync during testing.
- Make sure you invited the bot with the `applications.commands` scope.

**"Message Content Intent" error:**
- Go to Discord Developer Portal → Bot → enable Message Content Intent.

**Google Sheets "permission denied":**
- Make sure you shared the spreadsheet with the service account email.
- Make sure Google Sheets API and Google Drive API are both enabled.

**Bot token exposed accidentally:**
- Go to Discord Developer Portal → Bot → **Reset Token** immediately.
- Update `DISCORD_TOKEN` in your `.env` / hosting provider.

**Bot processes the same logs twice:**
- This shouldn't happen — the database tracks processed message IDs. If it does, check that the `data/` directory persists across restarts (Docker volume is configured for this).

---

## Testing

```bash
python -m pytest tests/ -v
```

The parser tests validate all format variations and edge cases. The database tests verify duplicate detection and DM stats accumulation using an in-memory SQLite database.

---

## Log Format

The parser currently expects:

```
Username: PlayerName
Lore name: Character Name
EP: 2
TP: 1
MP: 3
Proof: [screenshot]
```

- **Username** is required
- At least one of **EP/TP/MP** is required
- **Lore name** is optional (but validated when present)
- **Proof** is completely ignored
- Case-insensitive, tolerant of spacing
- Negative values supported (e.g. `EP: -2`)

Once you provide real example messages from your Data Managers, the parser can be fine-tuned in `parser/log_parser.py` without touching anything else.
