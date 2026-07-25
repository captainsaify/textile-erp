# WhatsApp Trading ERP

ERP for a small trading business, operated from WhatsApp. Full spec in
[CLAUDE.md](CLAUDE.md) and [docs/](docs/).

## Prerequisites (installed once)

- Python 3.12 + [uv](https://docs.astral.sh/uv/) — backend
- PostgreSQL 16 running locally (`textile_erp_dev` database)
- Redis 7 running locally
- Node.js 18+ — the WhatsApp bridge
- Google Chrome — the bridge drives it headlessly

## First-time setup

```bash
cp .env.example .env            # then fill in values; BRIDGE_SHARED_SECRET: openssl rand -hex 24
uv sync                         # python deps
uv run alembic upgrade head     # create/migrate the database (includes seed data)
cd whatsapp-bridge && npm install && cd ..
```

Register each person the bot should respond to (everyone else is
silently ignored):

```bash
uv run python -m backend.cli create-user --name "Sarfaraz" --whatsapp +91XXXXXXXXXX --role owner
uv run python -m backend.cli create-user --name "Partner"  --whatsapp +91XXXXXXXXXX --role staff
```

## Running (two terminals)

**Terminal 1 — backend:**

```bash
cd ~/textile-erp
uv run uvicorn backend.main:app --port 8000
```

**Terminal 2 — WhatsApp bridge:**

```bash
cd ~/textile-erp/whatsapp-bridge
BRIDGE_SHARED_SECRET=$(grep '^BRIDGE_SHARED_SECRET=' ../.env | cut -d= -f2) npm start
```

First run prints a QR code — scan it from the bot's phone (WhatsApp →
Settings → Linked devices → Link a device). The login persists in
`whatsapp-bridge/session/`, so later runs reconnect automatically.

Then message the bot (or use the "Message yourself" chat if the bot
runs on your own number) — send `help`.

Only one bridge instance can run at a time; "EADDRINUSE: port 3001"
means one is already running (`lsof -ti:3001 -sTCP:LISTEN` to find it).

## Group usage

Messages from registered users work in any chat. To let the *bot
account's own* messages work inside the business group (self-bot mode),
set `BRIDGE_ALLOWED_CHATS` to the group's chat id — the bridge prints
`skipped chat 1203...@g.us` the first time you type in an unallowed
group, copy the id from there:

```bash
BRIDGE_ALLOWED_CHATS=1203...@g.us BRIDGE_SHARED_SECRET=... npm start
```

## Development

```bash
uv run pytest backend/tests/          # test suite (needs TEST_DATABASE_URL + local Redis)
uv run mypy backend/                  # strict typecheck
uv run ruff check backend/ alembic/   # lint
uv run alembic upgrade head           # apply migrations
```
