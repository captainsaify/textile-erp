# 28 — Sheets Everywhere

> Companion to [`27_Documents.md`](27_Documents.md), which established
> *that* every transaction has a sheet built from the database on
> request. This document is about the two things that turned out not to
> be true in practice: **a change nobody can see on the sheet is a change
> nobody was told about**, and **a sheet you cannot download from the
> place you are looking at does not exist.**

---

## 1. What went wrong

### 1.1 The correction was recorded and still invisible

Purchase `003` (Noor Traders, 12-07-2026, 52 lines) was corrected 19 times
— bales received short or in excess against what was billed. Every one
of those corrections is in `audit_logs`, every one of them is on the
generated sheet, and the partner reading the sheet concluded that
nothing had been recorded.

He was right to. This is what the sheet looked like:

```
row  2   S.NO | QTY | DESCRIPTION | CODE | LABEL | KG | T.KG | RATE | AMOUNT
row  3   1    | 21  | CHILDREN WINTER WEAR | 028 | MKD | 80 | 1,680 | 115 | 1,93,200
…
row 55   TOTAL              334                        26,720         30,72,800
row 57   Subtotal: 30,72,800.00
…
row 64   CHANGES
row 66   30-07-2026 16:58 · Sarfaraz · Receipt corrected — 55CT 800.000 → 960.000
…        (19 lines, to row 84)
```

Three separate failures, and only the third is about content:

1. **Nothing in the table says a row was touched.** Line 2 (`55CT`) reads
   `960.000` as if that is what was billed. It is not — 800 was billed,
   960 arrived.
2. **The evidence is below the fold.** Nine blank-ish rows past the
   TOTAL, in column A, in the same font as everything else. A bill is
   read top-down and stops at TOTAL.
3. **The heading does not warn you.** `Supplier: Noor Traders  Invoice:
   003  Date: 12-07-2026` is the same caption a bill with no corrections
   carries.

**Rule this establishes:** a modified document must announce itself
*before* the reader reaches the numbers, and each modified number must
carry its own marker. The audit trail at the bottom is the proof, not
the notice.

### 1.2 The website shows their sheet, not ours

`GET /purchases/{id}/scan` renders the photographed sheet in the detail
panel. Beside it sits a bare list of `line_no / code / description / qty
/ rate / total` — which is a database dump, not the sheet. So the one
place where the original and our version could be compared side by side
shows the original and something else.

### 1.3 You can download some sheets from some pages

| Surface | Per-row sheet | Whole-page sheet |
|---|---|---|
| Purchases | yes | **no** |
| Sales | yes | **no** |
| Ledger (cash/bank) | yes, on settlement rows | **no** |
| Parties | **no** | **no** |
| Money (receivables/payables) | **no** | **no** |
| Stock | — | **no** |

The summary exports exist (`ReportService`, six report types) but only
over WhatsApp, asynchronously, delivered as a chat attachment. There is
no way to get one from the page you are looking at, and the cash & bank
ledger has no export at all.

---

## 2. What gets built

### 2.1 The sheet announces its own corrections

Three additions to `purchase_sheet_template.py`, all of which no-op for
an unmodified bill so an untouched sheet looks exactly as it does today:

**A banner row**, immediately under the caption and above the column
headers, bold, only present when the bill has been changed:

```
Supplier: Noor Traders    Invoice: 003    Date: 12-07-2026
⚠ MODIFIED — 19 change(s) since confirmation, last 30-07-2026 17:07 by Sarfaraz. See CHANGES at the bottom.
S.NO | QTY | DESCRIPTION | …
```

**A `NOTE` column**, appended after `AMOUNT`, empty on untouched rows
and carrying the change on the rest:

```
S.NO | … | RATE | AMOUNT   | NOTE
2    | … | 115  | 1,10,400 | Received 800.000 → 960.000 (30-07)
```

It goes last deliberately. The nine columns before it are the layout the
partners have read for years; a tenth on the end is additive, and the
totals row is unaffected (`TOTALLED_COLUMNS` stays `QTY`, `T.KG`,
`AMOUNT`).

**The `CHANGES` block stays** exactly as it is. It is the full audit
record with times and names, and it is the right thing to have at the
bottom — it was only ever wrong as the *sole* notice.

Per-row notes are derived in `DocumentService`, not the template:

| Audit action | Rows it marks | Note |
|---|---|---|
| `purchase.receipt_corrected` | the line in `entity_id` | `Received {before.qty} → {after.qty} ({dd-mm})` |
| `purchase.rate_corrected` | every code listed in `after_state["codes"]` | `Rate {before.rate} → {after.rate} ({dd-mm})` |
| `sale.created.undone`, `purchase.confirmed.undone` | — | banner only; the row numbers did not change |

A line corrected twice shows both notes, oldest first, separated by
`; ` — the last one is not the whole story when someone corrected a
correction (`ANG` on bill 003 went `1360 → 800 → 1040`).

### 2.2 One document, three surfaces

Today `DocumentService.purchase()` builds a `PurchaseBill` and
immediately writes it to `.xlsx`. The `PurchaseBill` — caption, banner,
rows, notes, history — *is* the document; the workbook is one rendering
of it.

So it splits:

```
DocumentService._purchase_bill(org, id) -> PurchaseBill   # the document
                .purchase(org, id)      -> Document       # …as .xlsx
                .purchase_view(org, id) -> dict           # …as JSON
```

and the same for `sale` and `payment`. `*_view()` returns the column
headers, the rows as strings already formatted the way the sheet formats
them, the totals row, the notes and the history — so the browser renders
the sheet rather than re-deriving it, and the web view cannot disagree
with the file that downloads from the same page.

New endpoints, all `Cache-Control: no-store` (a document is current as
of now or it is misinformation):

```
GET /purchases/{id}/document
GET /sales/{id}/document
GET /payments/{reference}/document
```

### 2.3 The website shows our sheet beside theirs

The purchase detail panel becomes two panes plus a toolbar:

```
┌ 003 · Noor Traders ──────────────────────── [Sheet ⭳] [Scan ⭳] [Close] ┐
│ ⚠ MODIFIED — 19 changes, last 30-07-2026 17:07 by Sarfaraz           │
├───────────────────────────────┬──────────────────────────────────────┤
│ OUR SHEET                     │ ORIGINAL SCAN                        │
│ S.NO QTY DESCRIPTION … NOTE   │  [photo, click to zoom]              │
│  2   12  CH SWEATER  Received │                                      │
│                      800→960  │                                      │
│ …                             │                                      │
│ TOTAL 334      26,720  30,72,800                                     │
│ CHANGES  (19)  ▸ expand                                              │
└───────────────────────────────┴──────────────────────────────────────┘
```

Rows carrying a note are tinted, so the corrections are findable by eye
at 52 lines. The `CHANGES` list is collapsed by default and expands —
19 lines of audit trail must be *available*, not in the way.

Sales get the same panel without the scan pane (a sale is typed, never
photographed). Settlements get it from the Ledger and Money pages.

### 2.4 A download button on every page

A new synchronous export router. The existing async `POST
/reports/export` stays — it is what WhatsApp uses, and a WhatsApp export
genuinely has to be a background job. But a browser asking for a
15-row ledger should not have to poll:

```
GET /exports/purchases.xlsx?from=&to=
GET /exports/sales.xlsx?from=&to=
GET /exports/stock.xlsx
GET /exports/parties.xlsx?role=supplier|customer
GET /exports/statement.xlsx?kind=supplier|customer&party_id=&from=&to=
GET /exports/cashbook.xlsx?account=cash|bank&from=&to=
```

Each one creates the same `report_jobs` row the async path creates, runs
the same builder inline, and streams the file. Same code, same numbers,
same audit trail of who exported what — only the delivery differs. The
job row is still written, so an export from the browser is as traceable
as one from the chat.

**`cashbook` is a new report type.** The existing `ledger` report is the
*party* ledger — every supplier or customer with what they owe and how
old it is. The website's Ledger tab is the *cash and bank* ledger, which
has never had an export. `backend/reports/excel/cashbook_template.py`
writes it: date, type, in, out, running balance, note, and a `CANCELLED`
marker on rows that a reversal has undone, whose amounts are excluded
from the money-in / money-out totals for the same reason the dashboard
excludes them.

Where the buttons go:

| Page | Button |
|---|---|
| Purchases | `Download all` (header) · `Sheet` (per row, exists) |
| Sales | `Download all` (header) · `Sheet` (per row, exists) |
| Stock | `Download` (header) |
| Parties | `Download all` (header) · `Statement` (per row) |
| Party detail | `Statement ⭳` |
| Ledger | `Download` (header, cashbook) · `Sheet` (settlement rows, exists) |
| Money | `Statement` on every receivable and payable row |

---

## 3. What is deliberately not built

- **No stored sheet files.** Unchanged from `27_Documents.md` §1 and for
  the same reason: a file written at confirmation time is stale the
  moment a correction lands, and two copies circulate with nothing on
  either saying which is current.
- **No editing from the web.** Every button here downloads or displays.
  Mutation stays on WhatsApp (`CLAUDE.md` rule 5).
- **No PDF.** The partners work in Excel; a second output format is a
  second thing to keep in step for no gain today.
- **The scan is not re-rendered or annotated.** It is a photograph of
  what the supplier sent, and it must stay exactly that — the whole
  point of showing it beside our sheet is that one is evidence and the
  other is our arithmetic.

---

## 4. Tests

| Test | Pins |
|---|---|
| `test_documents.py::test_corrected_bill_carries_row_notes` | a receipt-corrected line's `NOTE` cell reads `Received 800.000 → 960.000` |
| `…::test_twice_corrected_line_shows_both` | `ANG` shows both corrections, oldest first |
| `…::test_clean_bill_has_no_banner_and_nine_columns` | an untouched bill is byte-comparable to today's layout |
| `…::test_rate_correction_marks_every_listed_code` | all 26 codes on bill 001 carry a rate note |
| `…::test_view_matches_workbook` | `purchase_view()` rows equal the workbook's rows |
| `test_exports.py::test_each_export_streams_a_workbook` | all six endpoints return a valid `.xlsx` |
| `…::test_cashbook_excludes_reversed_from_totals` | money-in/out ignore cancelled rows |
| `…::test_export_writes_a_job_row` | a browser export is as traceable as a chat one |
