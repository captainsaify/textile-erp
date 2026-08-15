# CLAUDE.md

# WhatsApp-Native Trading ERP — Master Build Instruction

> This document is the master instruction for Claude Code (or any engineer)
> building this system. It is intentionally short — it is an index and a
> set of non-negotiable rules. Every implementation detail lives in
> `docs/`. If this file and a doc file disagree, the doc file wins for
> implementation detail, and this file wins for philosophy/priority.

> **Learning this codebase rather than building it?** Read
> [`LEARN.md`](LEARN.md) — a 24-session syllabus, one session a day,
> with progress kept in `docs/learn/progress.md`.
>
> **To whoever is reading this as the assistant:** when Sarfaraz says
> "teach me today's session" (or anything like it), read
> `.claude/agents/codebase-tutor.md` and follow it **in the
> conversation**. Do not spawn a subagent for it — a subagent cannot ask
> him to explain something back and then wait for the answer, and that
> exchange is the session.

> **Picking up mid-build?** Read [`HANDOFF.md`](HANDOFF.md) first. It
> records current state, the build order, and — importantly — which
> remaining tasks must not be attempted by a smaller model. If you are
> not Opus, check `HANDOFF.md` §3 before starting.

## What this is

A production-grade ERP, operated entirely from WhatsApp, for a small
trading business currently run by two partners trading textiles (fabric
rolls, jogging pants fabric, etc. — see the sample sheets this spec was
derived from). It replaces the partners' Excel-based purchase sheets and
notebook ledgers.

**The system is deliberately not textile-specific.** The core domain
model (products, units, purchases, sales, inventory, accounting) is
generic. Textile is the *first configured product type*, not a
hard-coded assumption. See [`docs/00_ProjectVision.md`](docs/00_ProjectVision.md)
for why, and [`docs/03_Inventory.md`](docs/03_Inventory.md) for how
product types and OCR templates are configured rather than coded.

## Non-negotiable philosophy

1. **This is not a CRUD app.** Every mutating action must reason about
   whether it is *plausible* before saving it, and must tell the user
   what it found. See "Intelligent behaviors" below and
   [`docs/00_ProjectVision.md`](docs/00_ProjectVision.md#not-a-crud-app).
2. **Money is `NUMERIC`, never `FLOAT`.** No exceptions, anywhere —
   database columns, Python types (`Decimal`), API schemas.
3. **Every mutation is audited.** No table holding business data may be
   changed without a corresponding `audit_logs` row. No hard deletes on
   business tables — soft delete only.
4. **No placeholder code, no TODOs, no mocked business logic** ships to
   `main`. A feature is either fully implemented (including its edge
   cases and tests) or it is not merged.
5. **WhatsApp is the primary interface, not a bolt-on.** Every mutating
   feature must be usable start-to-finish from WhatsApp. The web
   dashboard (read-heavy) and REST API exist for reporting, admin, and
   future integrations — never as the *only* way to do something.
6. **Config over code for domain variation.** Product types, OCR column
   templates, and unit systems are rows in the database / entries in
   YAML seed files — not `if brand == "textile"` branches in Python.

## Tech Stack

- Python 3.12
- FastAPI (async)
- PostgreSQL 16
- SQLAlchemy 2.0 (async) + Alembic
- Redis 7 (cache, Celery broker, WhatsApp session state)
- Celery 5 (+ Celery Beat)
- PaddleOCR (primary) + Tesseract (fallback) + OpenCV (preprocessing)
- Pandas + openpyxl (reports, Excel export)
- Docker Compose (deployment unit)
- Nginx (reverse proxy, TLS termination)
- JWT auth (admin dashboard / API)
- WhatsApp Business Cloud API (Meta)

Full rationale for each choice: [`docs/01_Architecture.md`](docs/01_Architecture.md).

## Folder Structure

```text
textile-erp/
  backend/
    api/            # FastAPI routers — HTTP concerns only, no business logic
    services/        # Business logic, orchestration, "intelligent" checks
    models/           # SQLAlchemy ORM models
    schemas/          # Pydantic request/response schemas
    repositories/      # DB access, one repository per aggregate
    workers/           # Celery tasks
    ocr/                # OCR pipeline (preprocess, detect, extract, learn)
    reports/             # Report generators, Excel/PDF export
    core/                 # config, security, db session, logging setup
    tests/                # pytest suite, mirrors backend/ structure
  frontend/           # Admin dashboard (read-heavy, JWT-auth'd)
  docs/               # This spec. See index below.
  docker/             # Dockerfiles, docker-compose.yml, nginx.conf
  alembic/            # DB migrations
```

## Database Modules

See [`docs/02_Database.md`](docs/02_Database.md) for full SQL, indexes,
and the ER diagram. Table list (generic core, not textile-specific):

`organizations` (single row today, exists so multi-tenant SaaS is a
migration not a rewrite — see [`docs/18_FutureRoadmap.md`](docs/18_FutureRoadmap.md)) ·
`users` · `partners` · `suppliers` · `customers` · `brands` ·
`product_types` · `units` · `products` · `ocr_templates` ·
`ocr_learning_dictionary` · `purchase_headers` · `purchase_lines` ·
`sales_headers` · `sales_lines` · `inventory` · `inventory_movements` ·
`expenses` · `income` · `cash_ledger` · `bank_ledger` ·
`partner_capital` · `journal` · `audit_logs` · `attachments` ·
`whatsapp_sessions` · `settings`

Every business table: UUID PK, `org_id` FK, `created_at`, `updated_at`,
`created_by`, `deleted_at` (soft delete — see
[`docs/02_Database.md`](docs/02_Database.md#soft-delete)).

## OCR Pipeline (summary — full detail in [`docs/07_OCR.md`](docs/07_OCR.md))

1. Receive image/PDF via WhatsApp media webhook.
2. Deskew, denoise, crop (OpenCV).
3. Detect table structure.
4. OCR cells (PaddleOCR primary, Tesseract fallback on low confidence).
5. Resolve columns using the **active `ocr_template`** for the detected
   `product_type` (not hard-coded column names). For the textile
   template: Qty, Description, Code, KG, Total KG.
6. Ignore S.No, Label, Total columns per the template's `ignore_columns`.
7. Fuzzy-match codes/descriptions against `products` and the
   `ocr_learning_dictionary`.
8. Ask the user only for fields the template marks `required_manual`:
   Supplier, Brand, Invoice No., Purchase date, Purchase rate, Freight,
   Other charges.
9. Preview parsed table over WhatsApp; low-confidence cells are flagged.
10. On confirmation, save purchase; on correction, update the learning
    dictionary.
11. Update weighted-average inventory (see
    [`docs/03_Inventory.md`](docs/03_Inventory.md)).
12. Store original image as an `attachments` row, linked to the purchase.

## Inventory Rules (summary — full detail in [`docs/03_Inventory.md`](docs/03_Inventory.md))

- Weighted Average Cost, computed per `product_id` per `org_id`.
- Returns, damaged stock, and manual adjustments are all
  `inventory_movements` rows with a typed `movement_type` — never
  free-form edits to the `inventory` balance.
- Low stock alerts driven by per-product `reorder_level`.
- Duplicate invoice detection (fuzzy, not just exact match) — see
  [`docs/04_Purchases.md`](docs/04_Purchases.md#duplicate-detection).
- Multi-brand via `brand_id` FK.
- Multi-warehouse ready: `warehouse_id` on `inventory` and
  `inventory_movements` from day one (single default warehouse seeded;
  UI/commands can ignore it until a second warehouse exists).

## WhatsApp Commands

Full syntax/examples/errors/permissions for every command:
[`docs/08_WhatsApp.md`](docs/08_WhatsApp.md).

`purchase` · `receive` · `rate` · `sale` · `return` · `expense` · `income` · `capital` ·
`withdraw` · `received` · `paid` · `dashboard` · `summary` · `stock` ·
`stock CODE` · `supplier NAME` · `customer NAME` · `ledger` · `profit` ·
`cash` · `bank` · `search` · `edit` · `undo` · `delete` · `export` ·
`backup` · `restore` · `settings` · `login as test` · `demo` · `reset demo` · `help`

## Intelligent behaviors (this is the point of the system)

These are mandatory, not aspirational. Each is specified with its
trigger, threshold, and message copy in the linked doc.

- Detect duplicate invoices automatically — [`04_Purchases.md`](docs/04_Purchases.md#duplicate-detection)
- Learn OCR corrections over time — [`07_OCR.md`](docs/07_OCR.md#learning-dictionary)
- Suggest corrections when OCR confidence is low — [`07_OCR.md`](docs/07_OCR.md#confidence-scoring)
- Detect suspicious transactions — [`14_Security.md`](docs/14_Security.md#suspicious-transaction-detection)
- Detect inventory mismatches — [`03_Inventory.md`](docs/03_Inventory.md#mismatch-detection)
- Alert when stock goes negative — [`03_Inventory.md`](docs/03_Inventory.md#negative-stock)
- Alert when sale price is below average purchase cost — [`05_Sales.md`](docs/05_Sales.md#below-cost-warning)
- Warn if invoice total doesn't match sum of line items — [`04_Purchases.md`](docs/04_Purchases.md#total-mismatch)
- Prevent duplicate sale entries — [`05_Sales.md`](docs/05_Sales.md#duplicate-sale-detection)
- Detect accidental repeated WhatsApp messages — [`08_WhatsApp.md`](docs/08_WhatsApp.md#message-deduplication)

## Documentation Index

| File | Contents |
|---|---|
| [docs/00_ProjectVision.md](docs/00_ProjectVision.md) | Mission, personas, non-goals, why product-agnostic |
| [docs/01_Architecture.md](docs/01_Architecture.md) | System design, component diagram, tech rationale |
| [docs/02_Database.md](docs/02_Database.md) | Full ER diagram, SQL DDL, indexes, migrations |
| [docs/03_Inventory.md](docs/03_Inventory.md) | Weighted average cost, movements, mismatch detection |
| [docs/04_Purchases.md](docs/04_Purchases.md) | Purchase flow, duplicate detection, freight allocation |
| [docs/05_Sales.md](docs/05_Sales.md) | Sale grammar, receivables, returns, below-cost checks |
| [docs/06_Accounting.md](docs/06_Accounting.md) | Ledgers, partner capital, P&L, cash flow |
| [docs/07_OCR.md](docs/07_OCR.md) | Full OCR pipeline, learning dictionary |
| [docs/08_WhatsApp.md](docs/08_WhatsApp.md) | Every command, state machine, webhook handling |
| [docs/09_AI.md](docs/09_AI.md) | Natural language query engine |
| [docs/10_API.md](docs/10_API.md) | REST API for dashboard/integrations |
| [docs/11_BackgroundWorkers.md](docs/11_BackgroundWorkers.md) | Celery tasks, schedules, retry policy |
| [docs/12_Dashboard.md](docs/12_Dashboard.md) | Dashboard data model, caching |
| [docs/13_Reports.md](docs/13_Reports.md) | Report types, Excel/PDF export |
| [docs/14_Security.md](docs/14_Security.md) | RBAC, audit, suspicious-activity detection |
| [docs/15_Testing.md](docs/15_Testing.md) | Testing strategy, coverage targets |
| [docs/16_Deployment.md](docs/16_Deployment.md) | Docker Compose, Nginx, backup/restore |
| [docs/17_CodingStandards.md](docs/17_CodingStandards.md) | Patterns, linting, PR checklist |
| [docs/18_FutureRoadmap.md](docs/18_FutureRoadmap.md) | Multi-tenancy, new product types, roadmap |
| [docs/19_InteractiveMessages.md](docs/19_InteractiveMessages.md) | WhatsApp buttons and list menus, and their limits |
| [docs/20_ConversationalIntake.md](docs/20_ConversationalIntake.md) | Photo → intent → gap analysis → one question at a time |
| [docs/21_WebDashboard.md](docs/21_WebDashboard.md) | Read-heavy dashboard, chart forms, deployment on the domain |
| [docs/22_GroupBroadcast.md](docs/22_GroupBroadcast.md) | Posting to the partners' group, and telling each partner directly with the sheet |
| [docs/23_ReceiptCorrections.md](docs/23_ReceiptCorrections.md) | When fewer bales arrive than were billed |
| [docs/26_RateChanges.md](docs/26_RateChanges.md) | Correcting the price on a confirmed bill |
| [docs/27_Documents.md](docs/27_Documents.md) | A sheet for every purchase, sale and payment, rebuilt on request |
| [docs/28_SheetsEverywhere.md](docs/28_SheetsEverywhere.md) | Making a correction visible on the sheet, and a download on every page |
| [docs/29_DemoMode.md](docs/29_DemoMode.md) | A second, throwaway business to demonstrate in |
| [docs/30_VpsMigration.md](docs/30_VpsMigration.md) | Moving the whole thing to a VPS, and what deliberately stays behind |

## Coding Standards (summary — full detail in [`docs/17_CodingStandards.md`](docs/17_CodingStandards.md))

- Type hints everywhere, `mypy --strict` clean.
- Repository pattern; services orchestrate repositories; routes call
  services only.
- 95%+ test coverage on `backend/services` and `backend/ocr`.
- Pytest, structured logging (`structlog`), no `print`.
- No business logic in routes.

## Acceptance Criteria

- Handles OCR purchase sheets matching the reference samples
  (`wagdia textile company.xlsx`, `Textile_Inventory_Template.xlsx`)
  end-to-end via WhatsApp.
- Inventory always balances: `inventory.qty_on_hand` for every product
  always equals the signed sum of its `inventory_movements`, verified
  by a nightly reconciliation job (see
  [`11_BackgroundWorkers.md`](docs/11_BackgroundWorkers.md#reconciliation)).
- Duplicate invoices prevented (fuzzy, cross-field).
- WhatsApp-first workflow for every mutating command.
- One-command dashboards (`dashboard`, `summary`).
- Zero manual spreadsheet editing required post go-live.
- Adding a second product type (e.g., "grocery" or "hardware") requires
  only a new `product_types` row, a new `ocr_templates` row, and a new
  `units` seed — no core code changes.
