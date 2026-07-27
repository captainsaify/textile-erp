# Handoff — WhatsApp-Native Trading ERP

Written 2026-07-27 at the end of an Opus session; updated the same day
by a Sonnet session that completed §2a. Read this before touching
anything; it records decisions and traps that are **not** recoverable
from the code or from `docs/`.

`CLAUDE.md` is still the authority on philosophy, and `docs/` on
implementation detail. This file only says **where we are**, **what's
next**, and **what must not be built by a smaller model**.

---

## 0. Model policy — read this first

Most of the remaining work is mechanical and safe for **Sonnet**. A
short list is not, and the reason is specific:

> Every Opus-required task below can produce a **wrong answer that
> never raises**. A corrupted weighted-average cost, an unbalanced
> journal, or a reconciliation job that silently drifts will pass every
> test and look fine on WhatsApp — and be discovered months later as
> money that doesn't add up. Tasks where mistakes crash loudly are
> fine for Sonnet; tasks where mistakes are silent and financial are
> not.

**If you are Sonnet and the task you were just given appears in §3,
stop before writing code and tell the user:**

> ⚠️ This task (`<name>`) is marked Opus-required in HANDOFF.md §3 —
> reason: `<the reason given there>`. Run `/model opus` and re-ask, or
> tell me explicitly to proceed anyway.

Do not silently proceed. Do not partially implement it and leave the
hard part for later — `CLAUDE.md` rule 4 forbids half-built features on
`main`, and these are exactly the features where a partial
implementation is most dangerous.

---

## 1. Where things stand

**Working and verified end-to-end**, on real hardware with a real phone:

- 30-table schema, migrations, seeds. Migration head: `d5a71c93e806`
  (`partner_capital.status`/`.posted_at` per §2b, then
  `purchase_lines.returned_qty` per §3.1).
- WhatsApp transport via **Meta Cloud API** (the working one).
- **All 27 spec'd commands are built**: `purchase` `sale` `return`
  `received` `paid` `stock` (+ `stock CODE`) `search` `expense` `income`
  `cash` `bank` `edit` `undo` `delete` `export` `backup` `restore`
  `settings` `help`,
  plus `details` (the OCR follow-up step, not in the spec's command
  list), the reporting six from §2a (`dashboard` `summary` `profit`
  `supplier` `customer` `ledger`), and §2b's `capital` `withdraw`
  `approve` `reject`.
- OCR: local pipeline (OpenCV → table detect → Paddle/Tesseract) **and**
  Claude vision, vision-first with automatic fallback.
- Celery workers, Beat schedule, nightly reconciliation, Excel export,
  backup/restore, and the full Docker/Nginx deployment layer.
- 270 tests pass, fixed and random order. `mypy --strict` clean across
  128 files. `ruff` clean.

**Live OCR result on the user's real 26-item purchase sheet:** all 26
rows correct, confirmed independently — the costing quantities sum to
27,280 KG, which is the grand total printed on the sheet. Local OCR
managed 20/26 on the same image. Vision cost $0.054, took 18.1s.

**REST API is built** (2026-07-28): JWT auth with argon2 + revocable
refresh tokens, the error envelope, and read endpoints for dashboard,
products, inventory, movements, purchases, sales, ledgers, P&L and
report export/polling. 17 tests in `backend/tests/api/test_rest_api.py`,
mostly authorisation boundaries. `python -m backend.cli set-password`
grants an account dashboard access.

**What is NOT built:** `frontend/` is still empty. Three plans now
exist for the remaining work:
- [`docs/19_InteractiveMessages.md`](docs/19_InteractiveMessages.md) —
  **Phases 1 and 2 done.** Phase 3 (Flows) deliberately not built; doc
  20 supersedes it, see that doc's §11.
- [`docs/20_ConversationalIntake.md`](docs/20_ConversationalIntake.md) —
  photo → intent → one question at a time. **Now unblocked; this is the
  next task.**
- [`docs/21_WebDashboard.md`](docs/21_WebDashboard.md) — dashboard
  forms, no-build stack, deployment on `example.com`.

**Domain confirmed:** `example.com`. Pointing the Meta
webhook at it would end the cloudflared quick-tunnel fragility (§5)
permanently — worth doing regardless of the dashboard.

**Deployment: built and verified running.** Colima provides the
container runtime (`colima start`; Docker Desktop was avoided — it needs
an admin password and a GUI). `docker compose up -d` brings up all eight
services. Confirmed working:

- both images build; api 807MB, worker 1.17GB
- both run as uid 10001; no `.env` in either image; tesseract only in
  the worker; `pg_dump` in both (the backup path shells out to it)
- all seven migrations apply to a completely empty database
- api passes its compose healthcheck
- a task dispatched through the real Redis broker was executed by
  worker-scheduled and wrote its `reconciliation_runs` row
- HTTPS through nginx reaches the app, HTTP 301s to it, HSTS/nosniff/
  DENY are set, and the webhook route 403s a wrong verify token

Local run needs `docker/certs/{fullchain,privkey}.pem` — self-signed is
fine (`openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout
docker/certs/privkey.pem -out docker/certs/fullchain.pem -subj
/CN=localhost`). That directory is gitignored.

**Two traps this shook out, both now pinned by tests:**

1. **`/app` is root-owned on purpose** — the app must not be able to
   rewrite its own code. Anything needing to write goes under `/data`.
   Celery Beat defaults its schedule file to the working directory and
   crash-looped on EACCES until pointed at `/data/celery`. If you add a
   process that writes to disk, give it a `/data` path.
2. **Compose must stay at the repo root.** It reads `.env` for
   interpolation from its own directory; under `docker/` every
   `${POSTGRES_USER}` resolved empty and Postgres started with no
   username.

---

## 2. Sonnet-safe work — ✅ all done

Everything in this section is built. **What remains is §3, which is
Opus-required.** If you are Sonnet and asked to "continue", the honest
answer is that the next task needs a model switch — say so rather than
picking the least-dangerous-looking item from §3.

**All of §3 is done**, along with the deployment layer. The only work
left is §8 below: the REST API and the web dashboard.

### 2a. The reporting six — ✅ done (2026-07-27, Sonnet)

`dashboard` · `summary` · `profit` · `ledger` · `supplier NAME` ·
`customer NAME` are built, registered, and tested (24 new tests in
`backend/tests/api/test_report_commands.py`).

What's there, for whoever touches this next:
- `backend/services/profit_service.py` — P&L from the journal's account
  rollup (`JournalRepository.account_rollup`), not re-derived from
  `expenses`/`income`/`sales_headers`. This is deliberate: whatever a
  service posts to the journal *is* the P&L, so a new transaction type
  can't quietly diverge the two the way a second hand-written
  calculation could.
- `backend/repositories/report_repository.py` — period totals,
  org-wide receivables/payables (one grouped query each), top sellers,
  slow-moving stock. `slow_moving_days` reads the `settings` table
  directly (default 60) since the `settings` command doesn't exist yet
  — when it ships, values it writes are picked up here with no change.
- `backend/repositories/party_repository.py` — `SupplierRepository`/
  `CustomerRepository.stats()` (aging buckets 0-30/31-60/61-90/90+,
  computed per open invoice, not a lump sum) and `.statement()` (the
  `ledger` command's event reconstruction from purchase/sales headers
  + cash/bank ledger rows — there's no separate per-payment table).
  **Payment sign is not symmetric between the two**: a supplier
  payment's effect on cash and on payable are both decreases (same
  sign, reused directly); a customer payment's effect on cash is an
  increase but on receivable is a decrease (opposite sign, negated).
  Both are commented in place — don't "simplify" one to match the
  other without re-deriving which way the sign actually goes.
- **Redis caching from docs/12_Dashboard.md §4 is NOT wired in.** Every
  dashboard/summary read hits Postgres directly every time — correct,
  just not fast. This was a deliberate scope cut, not an oversight: the
  spec's own graceful-degradation path already allows "cache
  unavailable → compute directly," so this is that path taken
  unconditionally. Wiring invalidation into every mutating service
  (purchase/sale confirm, payment, expense/income, capital, inventory
  adjustment) is real, separate, cross-cutting work — and a missed
  invalidation call would be exactly the kind of silent staleness
  `CLAUDE.md` tries hard to avoid everywhere else. Do this as its own
  focused pass, not bolted onto an unrelated change.

### 2b. `capital` / `withdraw` — ✅ done (2026-07-27, Opus)

`capital` `withdraw` `approve withdraw <id>` `reject withdraw <id>`,
plus `backend/services/capital_service.py` and 30 tests in
`backend/tests/api/test_capital_flow.py`.

This turned out to need a schema change and a design call the spec left
open, so read this before touching partner capital:

**A pending withdrawal must move nothing.** docs/06_Accounting.md §8
describes modelling it as a `partner_capital` row with
`approved_by_partner_ids` empty — but rows in that table form a
running-balance chain, so a pending one sitting in the chain would drop
equity while assets stayed put and break the balance-sheet identity in
§6 until someone answered. Hence `status` + `posted_at` (migration
`c8e2f0b41d73`):

- `balance()` reads only `status='posted'` rows;
- **the chain is ordered by `posted_at`, not `created_at`** — an
  approval can land long after the request, and the balance has to
  follow the order money actually moved. There is a test asserting
  `created_at` ordering *would* have been wrong; don't "simplify" the
  ordering back;
- approval recomputes against the balance at approval time, so a
  contribution that arrives while the request waits isn't overwritten;
- rejected requests are kept, not deleted.

`CommandResult.notifications` was added for this (the approval request
is the first message that must reach someone other than the sender).
It's best-effort by design: the transaction is already committed when
the fan-out runs, so a send failure is logged, never raised.

`SettingsRepository` also landed here — typed, defaulting reads of the
`settings` table. The `settings` *command* still doesn't exist (§2c),
but three thresholds already read from it.

### 2c. `settings` — ✅ done (2026-07-27, Opus)

`backend/core/settings_registry.py` is now the single home of every
default; services read through `SettingsRepository` rather than holding
their own constant, and a test asserts each accessor returns the
registry value when nothing is stored.

**Only keys something actually reads are registered** (7 today).
docs/ names about twenty; the rest — `backup_retention_days`,
`report_link_expiry_days`, `undo_window_hours`, `week_start_day`,
`large_adjustment_value_threshold`, `low_stock_check_hour`, the OCR
thresholds — are deliberately absent, because a key a partner can set
that changes no behaviour is a placeholder pretending to be a feature.
**When you build a feature that reads one, add its key to the registry
in the same change.** That's the intended growth path, not an oversight
to "fix" by bulk-adding the missing keys.

`base_currency` / `timezone` stay columns on `organizations` and must
not become settings rows: `business_today()` reads the column, so a
second source of truth would silently date entries wrong.

Note `below_cost_sale_tolerance_percent` is entered as a percent and
consumed as a fraction (`below_cost_tolerance()` divides by 100) —
there's a test pinning that 100x boundary.

---

## 3. Opus-required work — all complete

Kept in full below: the reasoning is what a future change to any of
these has to respect, and each entry names the branch that must not be
"simplified". The escalation rule in §0 still applies to *new* work of
the same shape.

### 3.1 `return` — ✅ done (2026-07-27, Opus)

`backend/services/return_service.py`, 25 tests in
`backend/tests/api/test_return_flow.py`. Read before touching costing:

- **A sale return must not recompute the average.** Stock returns at
  `sales_lines.avg_cost_at_sale_time`; that snapshot column exists
  precisely so reversing an old sale can't distort today's costing.
- **A purchase return unwinds the average and sometimes can't.** The
  exact form is the inverse of the §2 formula against the line's
  original landed cost. Once most of the batch has been sold and mixed
  with later purchases, remaining value or quantity goes to zero or
  below and no exact answer exists. In that case
  `record_purchase_return_movement` holds the average, lets quantity
  fall, and returns `approximated=True`, which the reply surfaces as a
  manual-check warning. **Do not "fix" that branch into always
  computing a number** — a negative or absurd average corrupts the cost
  basis of every later sale and nothing downstream raises. Tests pin
  both the exact case and the never-negative guarantee.
- **The refund question is not optional.** An already-paid cash sale
  parks in `AWAITING_RETURN_REFUND_CHOICE` and posts nothing until the
  partner answers; a test asserts neither stock nor cash moves while
  the question is outstanding.
- Preview and execute deliberately use **separate sessions** — execute
  opens its own transaction and re-validates, so a refund parked for
  minutes can't act on stale quantities.

### 3.2 `edit` / `undo` / `delete` — ✅ done (2026-07-27, Opus)
**Why:** each must reverse inventory movements *and* post compensating
journal entries *and* soft-delete only *and* stay auditable. They are
inverses of each other, so they have to be designed together — building
`delete` alone will produce an `undo` that can't undo it. This is the
subtlest work left in the project.
Spec: `docs/04_Purchases.md`, `docs/06_Accounting.md`, `docs/14_Security.md`.

### 3.3 Celery + reconciliation — ✅ done (2026-07-27, Opus)

`backend/workers/` and `backend/services/reconciliation_service.py`.
The rule to preserve: **reconciliation detects, it never repairs.**
Tests tamper with a cached balance and assert both that the mismatch is
reported *and* that the wrong number is still there afterwards. A
"helpful" auto-correct would destroy the only evidence that something
upstream is broken. A successful run is recorded too — without the
`reconciliation_runs` row, "nothing was wrong" and "the job never
fired" are the same silence.

### 3.4 Excel export — ✅ done (2026-07-27, Opus)

`backend/reports/excel/`. The purchases sheet keeps the partners'
column order (S.NO | QTY | DESCRIPTION | CODE | LABEL | KG | T.KG) with
a bold totals row; a test pins that order so a refactor can't drift it.
`COLUMNS` is a module constant in the same config-over-code shape as
`ocr_templates`, so a second product type brings its own list.

### 3.5 Any migration touching money/quantity columns or partitions
*(still applies to future migrations)*
**Why:** `NUMERIC` precision changes and partitioned-table DDL are
one-way in production. See §5 for the `alembic check` trap.

### 3.6 `backup` / `restore` — ✅ done (2026-07-27, Opus)

`backend/services/backup_service.py`. **Verify before pruning**: the new
dump is checksummed and its archive table-of-contents read back with
`pg_restore --list` before any old backup is deleted, because a pg_dump
that exits 0 having written a truncated file is the failure that stays
invisible until the day it matters. `restore` requires the backup's
name typed twice.

---

## 4. Dependency ordering (don't discover this the hard way)

- `export` needs **`backend/reports/` AND Celery**. It is specified as
  an async job returning a signed link, not a synchronous command. Both
  must exist first.
- `undo` is the inverse of `edit`/`delete` → build all three together.
- `dashboard`/`summary` are specified with caching
  (`docs/12_Dashboard.md`) but Redis is already wired
  (`backend/core/redis.py`) — no new infrastructure needed.
- `return` depends on the sale/purchase paths, both of which are done.

---

## 5. Traps and hard-won context

**`alembic check` reports ~196 differences. This is not drift.** They
are all monthly partitions created dynamically at runtime. Confirmed:
zero real diffs on `products` or `purchase_lines`. Don't "fix" it by
autogenerating a migration — you'll drop live partitions. Filter for the
table you actually changed.

**Transaction ordering.** `session.begin()` must open *before* any read,
or SQLAlchemy autobegins and you get *"A transaction is already begun on
this Session"*. This bit three separate times (money_service,
ocr_service, tests). Open the transaction first.

**Test data purging is opt-in, not global.** Each test file that writes
business rows declares its own `clean` fixture calling
`purge_business_rows`. If you add a test file that writes rows and skip
this, you'll leak rows into unrelated tests — and it may only surface in
random order. `brands` was missing from the purge order until this
session; add new tables to `_PURGE_ORDER` in `backend/tests/conftest.py`
**children before parents**.

**Redis singleton binds to the first event loop that touches it.**
pytest-asyncio gives each test a fresh loop; the autouse
`_reset_global_redis` fixture in `backend/tests/api/conftest.py` handles
it. Don't remove it.

**whatsapp-web.js is dead upstream.** The `whatsapp-bridge/` Node relay
broke when WhatsApp Web rolled a new build (`Client.inject` failure);
pinning versions and `protocolTimeout` all failed. **Meta Cloud API is
the working transport** (`WHATSAPP_TRANSPORT=meta`). The bridge code is
kept because the user wants WhatsApp *group* support eventually, which
Meta doesn't offer. Don't delete it, don't try to revive it without
checking upstream first.

**Meta webhook gotcha that cost hours:** a WABA can be subscribed to
Meta's own `WA DevX Webhook Events 1P App` instead of your app, and
messages then silently never arrive. Fix is
`POST /{waba-id}/subscribed_apps`. If inbound messages stop, check this
before anything else.

**OCR cross-check philosophy.** `_costing_quantity` used to prefer the
computed `qty × kg` over the sheet's stated total when they disagreed —
this silently corrupted two real rows (a correct 1520 became 28800). It
now **keeps what the sheet says and asks the user**. Do not "improve"
this back into auto-correction. When two sources disagree and you can't
tell which is wrong, surface it — that's the whole thesis of the
project (`CLAUDE.md`, "not a CRUD app").

**Header fuzzy threshold is 0.65, not the 0.7 in `docs/07_OCR.md` §5.**
Denoising turns a real sheet's "QTY" into "Qry" (ratio 66.7) and the
quantity column vanished. Documented in `backend/ocr/extract.py`.

**Product codes are brand-scoped as of `b3d1c7a9e42f`.** The same code
under a different brand is a *different product*. `get_by_code` returns
`None` when a bare code is ambiguous — callers must handle that, not
assume a hit. Use `list_by_code` when you need all carriers.

---

## 6. Loose ends

- **Vision returns empty supplier/invoice/date on the user's sheet.**
  Correct behaviour — that sheet doesn't print them; they're collected
  in the `details` step. Not a bug.
- **`ANTHROPIC_API_KEY` is live in `.env`** (gitignored). Vision is
  enabled (`OCR_USE_VISION=true`, `VISION_MODEL=claude-opus-5`). Costs
  ~$0.05/sheet. Setting the key empty cleanly falls back to local OCR.
  This is the *runtime* OCR budget — unrelated to build cost.
- **Vision latency is ~18s.** It runs as a background task, so the user
  sends a photo and gets the preview ~20s later. If that becomes a
  complaint, an ack-then-result message is the fix, not a faster model.
- **`docs/07_OCR.md` still describes only Paddle/Tesseract.** The vision
  engine is documented in code (`backend/ocr/vision_engine.py`) but the
  spec hasn't been updated to match. Worth reconciling.
- The user has explicitly accepted the whatsapp-web.js ToS/ban risk and
  chose to use their own number. Don't re-litigate it.

---

## 7. Definition of done (from `CLAUDE.md`)

Not done until: inventory always balances under the nightly
reconciliation job; every mutating command is usable start-to-finish
from WhatsApp; zero manual spreadsheet editing post go-live; and adding
a second product type needs only new rows, not new code.

The last one is worth re-testing once `export` exists — export templates
are the most likely place for textile assumptions to have leaked in.


---

## 8. What's left — the REST API and web dashboard

Everything in §2 and §3 is built. What remains is one piece of work in
two layers, and it is **Opus-optional** — nothing here can produce a
silently wrong number, because it is all read paths over figures the
WhatsApp side already computes.

### 8.1 REST API (`docs/10_API.md`)

Does not exist. Today's HTTP surface is `/healthz`, the WhatsApp webhook
and the bridge endpoints — nothing else. Needed: JWT auth
(`jwt_signing_key` is already in config and unused), then read endpoints
for dashboard, stock, ledgers, reports and the report-job polling route
`report_jobs` was designed for.

**Reuse the services, don't reimplement.** `DashboardService`,
`ProfitService`, `StockService` and the repositories already produce
every figure the dashboard needs; an endpoint that recomputes one of
them its own way is how the web dashboard and WhatsApp start disagreeing
about today's profit, which docs/12_Dashboard.md §1 explicitly forbids.

Two endpoints the backend is already shaped for but doesn't expose:
`POST /inventory/reconcile` (acknowledge a mismatch — see
`reconciliation_runs.acknowledged_at`, written for it and never yet set)
and `GET /reports/export/{job_id}`.

### 8.2 Web dashboard (`frontend/`, `docs/12_Dashboard.md`)

Empty directory. Read-heavy admin views; the compose file has no
frontend service yet either (the doc's illustrative one referenced a
Dockerfile that was never written — add both together).

Remember the ordering constraint from `CLAUDE.md` philosophy #5: the
dashboard is read-heavy *by design* and must never become the only way
to do something. Every mutating action stays available on WhatsApp.

### 8.3 Also outstanding

- **The Docker images have never been built.** See §1.
- **Redis caching for the dashboard** (docs/12_Dashboard.md §4) is still
  not wired in — see the note in §2a. Do it as its own focused pass.
- `docs/07_OCR.md` still describes only Paddle/Tesseract; the Claude
  vision engine is documented in code but not in the spec.
