# 13 — Reports

## 1. Report types

Per [`CLAUDE.md`](../CLAUDE.md): Daily, Weekly, Monthly, Quarterly,
Yearly, Custom (arbitrary date range) — all parameterizations of the
same underlying report generators, not separate implementations:

| Report | Contents | Backing service |
|---|---|---|
| Purchases | Line-item detail, per-supplier subtotal, freight/other breakdown | `PurchaseService.report(range)` |
| Sales | Line-item detail, per-customer subtotal, margin per line | `SalesService.report(range)` |
| Stock | Point-in-time (or as-of-date) qty + value per product | `InventoryService.report(as_of)` |
| P&L | Per [06_Accounting.md §5](06_Accounting.md#5-profit--loss) | `ProfitService.report(range)` |
| Balance Sheet | Per [06_Accounting.md §6](06_Accounting.md#6-balance-sheet-basic) | `BalanceSheetService.report(as_of)` |
| Cash Flow | Per [06_Accounting.md §7](06_Accounting.md#7-cash-flow) | `CashFlowService.report(range)` |
| Ledger (customer/supplier/product) | Full transaction history + running balance | `LedgerService.report(entity, range)` |
| Audit | Filtered `audit_logs` extract | `AuditService.report(filters)` |

## 2. Generation pipeline

```mermaid
flowchart LR
    A[Trigger: WhatsApp\n"export" or API\nPOST /reports/export] --> B[report_jobs row\nstatus=queued]
    B --> C[Celery: report_generation task]
    C --> D[Service layer query\n(same methods as\ndashboard/WhatsApp)]
    D --> E{format}
    E -- excel --> F[Pandas DataFrame\n-> openpyxl workbook]
    E -- csv --> G[Pandas DataFrame\n-> CSV]
    E -- pdf --> H[Jinja2 HTML template\n-> WeasyPrint PDF]
    F --> I[Upload to storage,\nreport_jobs status=ready]
    G --> I
    H --> I
    I --> J[WhatsApp: send as document attachment\n/ API: signed download URL]
```

## 3. `summary` vs. full reports {#summary-vs-full-reports}

`summary` (WhatsApp command,
[08_WhatsApp.md #summary](08_WhatsApp.md#summary)) is a **synchronous,
condensed digest** — computed inline (cache-backed, same as
dashboard), no file generated, answered in the chat directly within
the 3-second target. `export`
([08_WhatsApp.md #export](08_WhatsApp.md#export)) is the **asynchronous,
full-fidelity** report as a downloadable file — these are
deliberately different code paths for different needs (a quick glance
vs. a document to file/forward/print), not the same generator with a
"verbose" flag, because the synchronous path's latency budget forbids
the file-generation machinery entirely.

## 4. Report period boundaries

All periods resolve through the shared
`business_day_bounds(org, date)` helper referenced in
[02_Database.md §8](02_Database.md#8-timezone-handling) — "today,"
"this week" (Monday-start, configurable via
`settings.week_start_day`), "this month," "this quarter," "this year"
are all computed from the org's local calendar, never UTC-day
boundaries, so a report run at 11:50 PM never mislabels the next few
minutes' transactions into the wrong day.

## 5. Excel export format compatibility {#excel-export-format-compatibility}

The **Purchases export** must byte-for-byte match the partners'
existing sheet convention, per
[`CLAUDE.md`](../CLAUDE.md#excel-compatibility):

```
S.NO | QTY | DESCRIPTION | CODE | LABEL | KG | T.KG
```
with a totals row at the bottom (`SUM(QTY)`, `SUM(KG)`, `SUM(T.KG)`),
generated via `openpyxl` (not `pandas.to_excel`'s default styling,
which doesn't preserve exact column widths/borders/bold-totals-row
formatting the partners are used to) — a dedicated
`backend/reports/excel/purchase_sheet_template.py` builds the workbook
cell-by-cell against a captured reference layout derived from the
sample files (`wagdia textile company.xlsx`,
`Textile_Inventory_Template.xlsx`), with a visual-diff test in CI
against a golden-file fixture (see
[15_Testing.md](15_Testing.md#excel-golden-file-tests)) so a
dependency upgrade or refactor can never silently drift the output
format the partners rely on.

**This format is itself template-driven**, per the product-agnostic
core ([00_ProjectVision.md §4](00_ProjectVision.md#4-why-the-core-is-product-agnostic-not-textile-specific)):
the column layout above is the **textile product type's** default
export template (`export_templates` — same `product_type_id` keying
pattern as `ocr_templates`), not a hard-coded format; a future
non-textile product type ships its own export template without
touching this code path's structure, only its configured column list.

**Other exports** (sales, stock, ledgers, P&L) use a consistent,
generic tabular layout (header row, data rows, totals row, autosized
columns, currency/number formatting via `openpyxl` number formats
matching `en-IN` grouping, e.g. `₹1,23,456.00`) — not held to a
legacy-format constraint since there's no pre-existing partner sheet
convention for those.

## 6. PDF generation

Used for P&L/Balance Sheet/Cash Flow (documents meant to be
read/printed/shared as a formatted statement, not manipulated as data)
via Jinja2 HTML templates rendered to PDF with WeasyPrint — chosen
over a lower-level PDF library because report templates are
essentially styled HTML tables, and WeasyPrint's CSS support makes
matching a clean, printable statement layout straightforward without
hand-computing PDF coordinates.

## 7. Scheduling

- **On-demand only** in v1: every report is triggered by an explicit
  `export`/API call, not auto-emailed/auto-sent on a schedule — per
  [00_ProjectVision.md §7](00_ProjectVision.md#7-non-goals-explicitly-out-of-scope-for-v1)'s
  spirit of not building speculative automation the partners haven't
  asked for. `nightly_backup` and reconciliation jobs
  ([11_BackgroundWorkers.md](11_BackgroundWorkers.md)) are scheduled
  *system* jobs, distinct from *reports* — this section is about
  partner-facing reports specifically.
- Scheduled report delivery (e.g., "email me the P&L every Monday") is
  a documented, low-effort roadmap addition
  ([18_FutureRoadmap.md](18_FutureRoadmap.md)) once there's a
  concrete need, reusing `report_generation` unchanged plus a new
  Beat schedule entry.

## 8. Example JSON — report job status

```json
{
  "job_id": "9c3f...",
  "type": "purchases",
  "format": "excel",
  "date_from": "2026-07-01",
  "date_to": "2026-07-31",
  "status": "ready",
  "download_url": "https://.../reports/9c3f....xlsx?sig=...",
  "expires_at": "2026-07-26T00:00:00Z",
  "created_at": "2026-07-25T14:20:11Z"
}
```

## 9. Failure scenarios

| Scenario | Behavior |
|---|---|
| Requested date range has zero transactions | Report still generates (empty body, headers + "No records found" + zero totals) rather than erroring — an empty-but-valid report is meaningfully different from a failed one. |
| Export requested for a period spanning a reconciliation-flagged mismatch (§ [03_Inventory.md §6](03_Inventory.md#6-mismatch-detection)) | Report generates normally but includes a footer note flagging the unresolved mismatch, so a report pulled during a known-discrepancy window is never presented as unconditionally authoritative. |
| Very large export (multi-year, thousands of rows) | Streamed/chunked generation (Pandas processes in date-range batches, appended to the workbook incrementally) rather than loading the entire dataset into memory at once — bounded memory regardless of range size. |
| Download link accessed after `expires_at` | `410 Gone` with a re-generate prompt (WhatsApp: "This export expired — reply 'export purchases july' to generate a fresh one"). Links expire (default 7 days, `settings.report_link_expiry_days`) since these are financial documents and indefinitely-live unauthenticated-adjacent links are an unnecessary standing exposure — see [14_Security.md](14_Security.md#signed-download-links). |

## 10. Performance considerations

- Report generation is always async (Celery), even for small ranges —
  no report path competes with the webhook's 5-second response budget
  ([01_Architecture.md §8](01_Architecture.md#8-idempotency-and-delivery-guarantees)).
- Generated files are cached by `(org_id, type, format, date_from,
  date_to)` for `settings.report_cache_minutes` (default 30) — a
  repeated identical export request within that window returns the
  already-generated file instead of regenerating, common when a
  partner re-requests the same monthly report a few times while
  reviewing it.
