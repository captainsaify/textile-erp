# 27. A document for every transaction

Every confirmed purchase, sale and settlement has a sheet. It arrives
in WhatsApp attached to the confirmation, and it can be downloaded from
any row on the dashboard. There is exactly one builder
(`backend/services/document_service.py`) behind both, so the chat and
the web cannot show different numbers.

## 1. Built on request, never stored

The document is generated from the database at the moment it is asked
for. Nothing is written at confirmation time and re-served later.

That is not an implementation convenience — it is the point:

- A bill whose rate was corrected (`docs/26_RateChanges.md`) or whose
  receipt came up short (`docs/23_ReceiptCorrections.md`) has exactly
  one current version, and it is the one this produces. A file written
  at confirmation would keep circulating with the superseded numbers
  while the correction lived only in a chat message.
- A cancelled bill says `STATUS: CANCELLED` on its own face.

The cost is a few hundred milliseconds of openpyxl per download, which
is the right trade against two spreadsheets in circulation and nothing
on either saying which is current.

## 2. Changes are printed on the document

Under the totals, a **CHANGES** block lists every audited change that
touched the transaction, oldest first:

```
CHANGES
19-07-2026 17:11 · Firoz · Purchase confirmed
30-07-2026 12:20 · Sarfaraz · Rate corrected — rate 100 → 107
31-07-2026 09:04 · Firoz · Receipt corrected (short/excess bales) — 35A 800 → 720
```

Read straight from `audit_logs`, which every mutation already writes
(`CLAUDE.md` rule 3) — the trail existed, it simply was not on the
paper anyone was looking at. A bill's corrections are recorded against
its *lines*, so the history query covers the header **and** its line
ids; omitting them would print a total nobody could account for.

## 3. What each document is

| Kind | Reference | Contents |
|---|---|---|
| Purchase | header id | The partners' own column layout (`S.NO · QTY · DESCRIPTION · CODE · LABEL · KG · T.KG · RATE · AMOUNT`), then subtotal/freight/other/grand total, paid and outstanding |
| Sale | header id | The same layout — it is the sheet they already know how to read — plus payment type, received and outstanding |
| Payment | audit entry, short form (`ec196ee8`) | A receipt: who, how much, cash or bank, and the bills it settled line by line. An advance says so |

QTY and KG on a purchase are derived: a line stores the costing
quantity (total KG) and the weight of one bale, and the sheet's QTY
column is the bale count.

## 4. Where it appears

**WhatsApp** — attached automatically to: purchase confirmed, sale
confirmed, `paid`, `received`, a rate correction, and a receipt
correction. `sheet` with no draft waiting returns the most recent
purchase or sale rather than "there's no draft waiting", which was true
and useless: the thing being asked for exists, it is just saved now.

Building a document never fails the command it decorates. A document
that could not be built is a missing attachment, never a purchase that
did not save.

**Dashboard** — a `Sheet` button on every row of Purchases, Sales and
the Ledger (payments only). Fetched with the bearer token rather than
linked, because a plain `href` drops the `Authorization` header and
comes back a 401.

## 5. Endpoints

```
GET /api/v1/purchases/{id}/sheet
GET /api/v1/sales/{id}/sheet
GET /api/v1/payments/{reference}/sheet
```

All three return `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
with `Cache-Control: no-store` — a cached copy of a document whose whole
promise is "current as of now" would defeat it.
