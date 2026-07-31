# WhatsApp Trading ERP

An ERP for a small trading business that is **operated entirely from
WhatsApp**. It replaces a pile of Excel purchase sheets and paper
ledgers: photograph a supplier's bill, answer a few questions in the
chat, and the purchase, the inventory movement, the payable and the
journal entries are all recorded.

The web dashboard is read-only by design. Anything that changes data
happens in the chat, because that is where the people using it already
are.

```
  📷 photo of a bill          "purchase"          dashboard
        │                         │                   │
        ▼                         ▼                   ▼
   OCR pipeline ──► conversational intake ──► PostgreSQL ──► read-only web
   (Paddle/Tesseract)   (one question at a time)   (Decimal money,
                                                    full audit trail)
```

## What makes it more than CRUD

Every mutating action reasons about whether it is *plausible* before
saving, and says what it found:

- **Duplicate invoices** are caught fuzzily and cross-field, not by exact
  match — [`docs/04`](docs/04_Purchases.md)
- **OCR learns from corrections**; a code you fix once is read correctly
  the next time — [`docs/07`](docs/07_OCR.md)
- **Selling below weighted-average cost** warns before it
  saves — [`docs/05`](docs/05_Sales.md)
- **Inventory is reconciled nightly** against the signed sum of its
  movements, and a mismatch is *reported, never repaired* — repairing it
  would destroy the evidence that something upstream is
  broken — [`docs/03`](docs/03_Inventory.md)
- **Nothing is ever deleted.** Corrections are compensating entries, so a
  reversed payment and the entry that reversed it both stay on the
  page — and neither is counted in "money out"
- **Every document says what changed about it** — a corrected bill
  carries a MODIFIED banner, per-row markers, and who changed what and
  when, read straight from the audit log — [`docs/28`](docs/28_SheetsEverywhere.md)

Two rules the whole codebase is built on: **money is `NUMERIC`/`Decimal`,
never a float**, anywhere — including the browser; and **every mutation
writes an `audit_logs` row**, with soft deletes only.

The domain model is deliberately generic. Textile is the first
*configured* product type, not a hard-coded assumption — adding a second
one is a `product_types` row, an `ocr_templates` row and a `units` seed,
with no core code changes.

## Stack

Python 3.12 · FastAPI (async) · PostgreSQL 16 · SQLAlchemy 2.0 async +
Alembic · Redis 7 · Celery 5 + Beat · PaddleOCR + Tesseract + OpenCV ·
openpyxl · Docker Compose · Nginx · whatsapp-web.js bridge (or the Meta
Cloud API)

Frontend is plain HTML/CSS/JS with no build step — the dashboard is
read-heavy and a toolchain would earn nothing.

## Quick start (Docker — everything at once)

```bash
cp .env.example .env      # fill in values; see the secrets list in docs/16
docker compose -f docker-compose.yml -f docker-compose.override.local.yml up -d
docker compose exec -T api python -m backend.cli create-user \
    --name "Owner" --whatsapp +911234567890 --role owner
```

The override file publishes Postgres, Redis and the API on host ports so
`psql` and `redis-cli` reach them; production leaves them inside the
compose network. API on `http://localhost:8000`, dashboard behind nginx,
health at `/healthz`.

## Quick start (local, no Docker)

<details>
<summary>Prerequisites and the two-terminal run</summary>

- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16 with a `textile_erp_dev` database
- Redis 7
- Node.js 18+ and Google Chrome — the bridge drives Chrome headlessly

```bash
cp .env.example .env            # BRIDGE_SHARED_SECRET: openssl rand -hex 24
uv sync
uv run alembic upgrade head     # migrate + seed
cd whatsapp-bridge && npm install && cd ..
```

Register everyone the bot should answer — **unregistered numbers are
silently ignored**, which is the entire access-control model:

```bash
uv run python -m backend.cli create-user --name "Owner"   --whatsapp +91XXXXXXXXXX --role owner
uv run python -m backend.cli create-user --name "Partner" --whatsapp +91XXXXXXXXXX --role staff
```

**Terminal 1 — backend:**

```bash
uv run uvicorn backend.main:app --port 8000
```

**Terminal 2 — WhatsApp bridge:**

```bash
cd whatsapp-bridge
BRIDGE_SHARED_SECRET=$(grep '^BRIDGE_SHARED_SECRET=' ../.env | cut -d= -f2) npm start
```

First run prints a QR code — scan it from the bot's phone (WhatsApp →
Settings → Linked devices → Link a device). The login persists in
`whatsapp-bridge/session/`, so later runs reconnect on their own.

Only one bridge instance may run at a time; `EADDRINUSE: port 3001`
means one already is (`lsof -ti:3001 -sTCP:LISTEN`).

</details>

Then message the bot and send `help`.

## Group usage

Messages from registered users work in any chat. To let the *bot
account's own* messages work inside the business group (self-bot mode),
set `BRIDGE_ALLOWED_CHATS` to the group's chat id. The bridge logs
`skipped chat 1203...@g.us` the first time you type in an unallowed
group — copy the id from there:

```bash
BRIDGE_ALLOWED_CHATS=1203...@g.us BRIDGE_SHARED_SECRET=... npm start
```

## A taste of the chat interface

```
purchase                     receive 003 55CT 12      paid Noor 500000 cash on 09-07
sale VVP 100 260             rate 001 107             received Rahim 25000 bank
stock VVP                    export                   statement Noor
dashboard                    summary                  undo
```

Full syntax, errors and permissions for every command:
[`docs/08_WhatsApp.md`](docs/08_WhatsApp.md).

## Layout

```
backend/
  api/           FastAPI routers + the WhatsApp command layer (HTTP/chat only)
  services/      business logic, orchestration, the "is this plausible" checks
  repositories/  DB access, one per aggregate
  models/        SQLAlchemy ORM
  ocr/           preprocess → detect → extract → learn
  reports/       Excel builders
  workers/       Celery tasks
  tests/         mirrors the tree above
frontend/        read-only dashboard, no build step
whatsapp-bridge/ Node relay for whatsapp-web.js
docker/          Dockerfiles, nginx, cloudflared
docs/            the specification — 28 documents, see the index in CLAUDE.md
alembic/         migrations
```

Routes call services; services orchestrate repositories; **no business
logic lives in a route**.

## Development

```bash
uv run pytest backend/tests/          # needs TEST_DATABASE_URL + local Redis
uv run mypy --strict backend/         # must be clean
uv run ruff check backend/ alembic/   # must be clean
uv run alembic upgrade head
```

573 tests. See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR —
the standards here are stricter than most projects', deliberately.

## Documentation

[`CLAUDE.md`](CLAUDE.md) is the master index: philosophy, non-negotiable
rules, and a table of all 28 documents. Start there. The ones people
reach for most:

| | |
|---|---|
| [00 Project Vision](docs/00_ProjectVision.md) | why it is product-agnostic |
| [01 Architecture](docs/01_Architecture.md) | components and why each tool |
| [02 Database](docs/02_Database.md) | ER diagram, DDL, indexes |
| [08 WhatsApp](docs/08_WhatsApp.md) | every command and the state machine |
| [16 Deployment](docs/16_Deployment.md) | compose, nginx, tunnel, backups |
| [17 Coding Standards](docs/17_CodingStandards.md) | patterns and the PR checklist |

[`HANDOFF.md`](HANDOFF.md) records current state and the traps that have
already cost someone a day.

## Status

Running in production for one business. Interfaces are still moving;
treat it as a working system rather than a stable library.

## Licence

[MIT](LICENSE). Use it, fork it, sell it — just keep the notice.

## Deploying it somewhere

The public hostname is not in this repo. Set `TUNNEL_HOSTNAME` in `.env`
and copy `docker/cloudflared/config.example.yml` to `config.yml` with
the same hostname — cloudflared has no environment substitution, so it
has to be literal in that file, which is why the real one is gitignored.
[`docs/16_Deployment.md`](docs/16_Deployment.md) §11 walks the whole
tunnel setup, and `./docker/tunnel-check.sh` tells you which link is
broken when the webhook goes quiet.
