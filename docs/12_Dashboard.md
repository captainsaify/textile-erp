# 12 — Dashboard

## 1. Two surfaces, one data model

The dashboard exists in two forms sharing one backend computation
layer (`DashboardService`) and one cache (§4):
1. **WhatsApp `dashboard`/`summary` commands** — the primary,
   day-to-day surface (per
   [`CLAUDE.md`](../CLAUDE.md#non-negotiable-philosophy)).
2. **Web admin dashboard** (`frontend/`) — richer, chart-heavy,
   read-heavy views for when a phone-sized text summary isn't enough
   (deep history, visual trends, side-by-side comparisons).

Both read from `GET /api/v1/dashboard`
([10_API.md §4](10_API.md#dashboard)) or the equivalent internal
service call — never two separate implementations of "what is
today's profit," per
[09_AI.md §1](09_AI.md#1-why-this-is-not-send-the-question-to-an-llm-with-db-access)'s
same reasoning about consistency.

## 2. Data displayed (per `CLAUDE.md`)

```
Cash · Bank · Inventory Value · Inventory Qty · Today's Sales ·
Today's Purchases · Monthly Profit · Outstanding Receivables ·
Outstanding Payables · Partner Capital · Top Selling Items ·
Slow Moving Stock · Low Stock
```

Each field's exact computation, already specified in its owning doc,
is reused here rather than redefined:

| Field | Source |
|---|---|
| Cash / Bank | Latest `resulting_balance` on `cash_ledger`/`bank_ledger` — [06_Accounting.md §9](06_Accounting.md#9-cash-vs-bank) |
| Inventory Value / Qty | `InventoryValuationService.total_value()` / `SUM(qty_on_hand)` — [06_Accounting.md §6](06_Accounting.md#6-balance-sheet-basic) |
| Today's Sales / Purchases | `SalesService.total(today)` / `PurchaseService.total(today)`, business-local day bounds — [02_Database.md §8](02_Database.md#8-timezone-handling) |
| Monthly Profit | `ProfitService.calculate(month-to-date)` — [06_Accounting.md §5](06_Accounting.md#5-profit--loss) |
| Outstanding Receivables / Payables | `LedgerService.total_receivables()` / `total_payables()` — [06_Accounting.md §10](06_Accounting.md#10-receivables--payables-aging) |
| Partner Capital | `PartnerCapitalService.balances()` — owner-only field, [06_Accounting.md §8](06_Accounting.md#8-partner-capital-accounting) |
| Top Selling Items | `SalesService.top_products(period=month, limit=5)`, ranked by revenue |
| Slow Moving Stock | Products with zero `sale` movements in `settings.slow_moving_days` (default 60) despite `qty_on_hand > 0` |
| Low Stock | `InventoryService.low_stock_list()` — [03_Inventory.md §7](03_Inventory.md#7-low-stock-alerts) |

## 3. WhatsApp dashboard output format {#whatsapp-dashboard-output-format}

```
📊 Dashboard — 25 Jul 2026, 14:32

💰 Cash: ₹18,500.00   🏦 Bank: ₹2,42,100.00
📦 Inventory: ₹4,52,300.00 (1,240.5 units across 23 products)

Today: 🛒 Sales ₹12,400.00 · 📥 Purchases ₹24,000.00
📈 Profit (Jul, MTD): ₹86,240.00

💸 Receivables: ₹42,300.00 (4 customers)
💳 Payables: ₹14,000.00 (2 suppliers)

🏆 Top sellers (this month): TRP, MJP, NEG
🐌 Slow moving: KLR (62 days no sale)
📉 Low stock: 3 items — reply "stock low" for detail
⚠️ Negative stock: 1 item — reply "stock negative"

[owner only:] 👥 Partner capital — Rahul ₹2,15,000 · Farida ₹1,98,400
```

`summary` is the same data condensed to fewer lines, tuned for a quick
glance rather than a full readout — full format in
[13_Reports.md §3](13_Reports.md#3-summary-vs-full-reports).

## 4. Caching strategy {#caching}

- All dashboard fields are computed and cached together as one Redis
  hash (`dashboard:{org_id}`), **60-second TTL**, per
  [01_Architecture.md §11](01_Architecture.md#11-performance--scalability-considerations)
  and the <3s response target in
  [00_ProjectVision.md §8](00_ProjectVision.md#8-success-metrics).
- **Explicit invalidation** on any write that affects a dashboard
  field (purchase/sale confirm, payment, expense/income, capital
  event, inventory adjustment) — the relevant service calls
  `DashboardCache.invalidate(org_id)` in the same transaction's
  post-commit hook, so the *next* read recomputes fresh rather than
  waiting out the TTL. The 60s TTL is a safety net (covers cache
  entries invalidation might miss due to a bug, and bounds staleness
  even under invalidation-path failure), not the primary freshness
  mechanism — the primary mechanism is invalidation-on-write.
- Partner-capital fields are cached and gated separately within the
  same hash (role-filtered at the read/format layer, not by a separate
  cache entry) — avoids fetching the whole dashboard twice for
  different roles while still enforcing RBAC on the response.

## 5. Web dashboard endpoints (beyond `/dashboard`)

The frontend additionally consumes the report, ledger, and list
endpoints cataloged in [10_API.md §4](10_API.md#4-endpoints) for
drill-down views (e.g., clicking "Low stock: 3 items" navigates to a
filtered `GET /api/v1/inventory?low_stock=true` table) — the dashboard
endpoint itself stays a fixed-shape summary, not a general-purpose
query endpoint, keeping its cache key and invalidation logic simple.

## 6. Failure scenarios

| Scenario | Behavior |
|---|---|
| Redis cache unavailable | `DashboardService` falls back to computing directly from Postgres (slower, but never fails outright) — the WhatsApp `dashboard` command degrades gracefully to a few extra seconds of latency rather than an error. |
| A dashboard field's underlying query times out (unlikely at current scale, guarded regardless) | That specific field renders as "unavailable" in its slot rather than failing the whole dashboard response — partial degradation, not all-or-nothing. |
| `dashboard` requested by `staff` | Partner-capital section is omitted entirely (not shown as "hidden" — simply absent), per RBAC in [14_Security.md](14_Security.md#rbac). |

## 7. Performance considerations

- Single Redis round-trip per dashboard read (one hash `HGETALL`), not
  N queries — the expensive aggregation work happens once, at write-
  invalidation time or on the rare cache-miss recompute, never
  repeated per read.
- Web dashboard's richer historical charts (trend lines beyond current
  snapshot) query pre-aggregated daily rollups (a lightweight
  `daily_org_metrics` materialized view refreshed nightly alongside
  reconciliation — not the live per-request path) rather than
  recomputing month-over-month trends from raw transactional tables on
  every page load.
