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

- 30-table schema, migrations, seeds. Migration head: `b3d1c7a9e42f`
  (no schema change since — §2a added no columns, only queries).
- WhatsApp transport via **Meta Cloud API** (the working one).
- 17 commands: `purchase` `sale` `received` `paid` `stock` (+ `stock
  CODE`) `search` `expense` `income` `cash` `bank` `help`, plus
  `details` (the OCR follow-up step, not in the spec's command list),
  plus the reporting six added in §2a below: `dashboard` `summary`
  `profit` `supplier` `customer` `ledger`.
- OCR: local pipeline (OpenCV → table detect → Paddle/Tesseract) **and**
  Claude vision, vision-first with automatic fallback.
- 142 tests pass, fixed and random order. `mypy --strict` clean across
  100 files. `ruff` clean.

**Live OCR result on the user's real 26-item purchase sheet:** all 26
rows correct, confirmed independently — the costing quantities sum to
27,280 KG, which is the grand total printed on the sheet. Local OCR
managed 20/26 on the same image. Vision cost $0.054, took 18.1s.

**Empty directories — nothing built yet:** `backend/workers/`,
`backend/reports/`, `frontend/`, `docker/`. Celery is not installed.

---

## 2. Sonnet-safe work, in the order I'd do it

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

### 2b. `capital` / `withdraw` ← **start here**

Partner capital in/out. Mutating, but there is direct precedent:
`backend/api/commands/money_commands.py` (`expense`/`income`) already
does ledger entry + double-entry journal posting + audit. Follow it
closely. Spec: `docs/06_Accounting.md`.

⚠️ If the journal doesn't balance in your tests, **stop and escalate** —
don't "fix" it by adjusting the expected value.

### 2c. `settings`

Key/value reads and writes on the `settings` table. The simplest
remaining command. Spec: `docs/08_WhatsApp.md`.

---

## 3. Opus-required — STOP and ask for a switch

### 3.1 `return`
**Why:** returning purchased stock must reverse **weighted-average
cost**. WAC is order-dependent and self-referential; a wrong reversal
silently corrupts the cost basis of every future sale, profit figure and
stock valuation, and nothing raises. Also interacts with the
`SELECT … FOR UPDATE` costing path in `inventory_service`.
Spec: `docs/03_Inventory.md`, `docs/05_Sales.md`.

### 3.2 `edit` / `undo` / `delete`
**Why:** each must reverse inventory movements *and* post compensating
journal entries *and* soft-delete only *and* stay auditable. They are
inverses of each other, so they have to be designed together — building
`delete` alone will produce an `undo` that can't undo it. This is the
subtlest work left in the project.
Spec: `docs/04_Purchases.md`, `docs/06_Accounting.md`, `docs/14_Security.md`.

### 3.3 Celery workers + nightly reconciliation
**Why:** the reconciliation job is a stated acceptance criterion in
`CLAUDE.md` — `inventory.qty_on_hand` must equal the signed sum of
`inventory_movements` for every product. It runs concurrently with live
mutations, against monthly-partitioned tables, and a subtly wrong query
reports "all balanced" forever while drift accumulates.
Spec: `docs/11_BackgroundWorkers.md`.

### 3.4 Excel export golden-file work
**Why:** `docs/13_Reports.md` §5 requires the purchases export to match
the partners' existing sheet *byte-for-byte* — column order, headers,
number formats — verified against `wagdia textile company.xlsx` by a
visual-diff test. Fiddly `openpyxl` format-matching against real files,
and the whole point is defeated by "close enough".
(The rest of `backend/reports/` — plumbing, the Celery job wrapper, the
signed download link — is Sonnet-safe. It's the workbook builder that
isn't.)

### 3.5 Any migration touching money/quantity columns or partitions
**Why:** `NUMERIC` precision changes and partitioned-table DDL are
one-way in production. See §5 for the `alembic check` trap.

### 3.6 `backup` / `restore`
**Why:** `restore` overwrites live business data. Destructive and
irreversible; wants the stronger model and an explicit human
confirmation step.

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
