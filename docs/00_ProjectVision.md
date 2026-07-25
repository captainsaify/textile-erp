# 00 — Project Vision

## 1. Mission

Build a production-grade ERP, operated entirely from WhatsApp, for a
small trading business run by two partners. Today the business tracks
purchases, sales, and stock in Excel sheets and paper ledgers. The
system replaces that workflow with WhatsApp commands and photos of
supplier invoices, while preserving the exact business logic the
partners already trust (weighted-average costing, the purchase-sheet
column layout, the way freight gets split across a purchase).

The reference domain is **textile trading**: fabric bought in rolls,
described by code and description, weighed in KG, with per-item and
total KG columns on the supplier's purchase sheet. Two real sample
sheets (`wagdia textile company.xlsx`, `Textile_Inventory_Template.xlsx`)
are the ground truth for what the OCR pipeline must parse correctly on
day one.

## 2. Why WhatsApp, not a web app or mobile app

- The partners already use WhatsApp all day, for business and personal
  messaging. Any other interface is a second app they have to remember
  to open.
- Purchase entry starts with a photo — WhatsApp is already the fastest
  path from "photo of an invoice" to "message sent."
- No app store, no login screen, no onboarding friction, works on
  low-end Android phones with unreliable connectivity (WhatsApp queues
  messages and retries; a web app does not, by default).
- A web dashboard still exists (see [12_Dashboard.md](12_Dashboard.md))
  for the cases WhatsApp is a bad fit — deep historical reports, bulk
  exports, chart-heavy views — but it is never required for the
  day-to-day workflow.

## 3. Why "not a CRUD application" {#not-a-crud-app}

A CRUD system trusts whatever the user types and stores it. That is
adequate for a system of record used by trained data-entry staff with a
supervisor checking their work. It is inadequate here, because:

- The people entering data are the business owners themselves, entering
  data quickly, on a phone, often between other tasks — the exact
  conditions under which typos and duplicate entries happen.
- There is no supervisor layer reviewing entries before they hit the
  books. The system has to be the reviewer.
- The cost of a silent error compounds: a duplicate purchase entry
  overstates both inventory and payables; it will not be caught until a
  physical stock count disagrees with the system, potentially months
  later, at which point the root cause is unrecoverable.

Therefore, every mutating flow in this system runs domain checks
*before* committing, and communicates its reasoning back to the user in
plain language, rather than just accepting or rejecting silently. The
system behaves like a careful accountant looking over the partner's
shoulder: "This invoice number looks like one you already entered on
15 March for the same supplier and amount — is this a duplicate?"
rather than either blindly saving it or blindly rejecting it. Every
such check is enumerated with its exact trigger and message in the
relevant domain doc, and cross-referenced from [`CLAUDE.md`](../CLAUDE.md#intelligent-behaviors).

## 4. Why the core is product-agnostic, not textile-specific

This system is being built for a real, specific textile trading
business — but hard-coding textile assumptions (KG as the only unit,
"Code" and "Description" as the only descriptive fields, a fixed
column layout) into the core domain model would be a mistake even for
that single business, for three reasons:

1. **Reality doesn't stay textile-only.** Traders in this segment
   commonly deal in adjacent goods (trims, packaging, accessories) that
   don't fit a KG-weighed-roll model. If the core model hard-codes
   "weight in KG," adding a piece-counted product means a schema
   migration and code branches, not a config change.
2. **The purchase sheet format is a supplier convention, not a law of
   physics.** Different suppliers already format sheets slightly
   differently. Treating "Qty, Description, Code, KG, Total KG" as *the*
   schema instead of *a* configured template means every new supplier
   layout is a code change instead of a new `ocr_templates` row.
3. **Reuse.** If this system proves itself for one trading business, the
   natural next step is running it for other small traders — a
   hardware dealer, a grocery distributor, an electronics reseller.
   None of them weigh their stock in KG or use a "Code + Description"
   sheet layout. A product-agnostic core means that expansion is a
   matter of adding `product_types`, `units`, and `ocr_templates` rows
   — not rewriting `products`, `purchase_lines`, `inventory`, or any
   accounting table.

**What "product-agnostic core" means concretely:**

- The `products` table (see [02_Database.md](02_Database.md#products))
  has no textile-specific columns. It has `code`, `description`,
  `brand_id`, `category_id`, `unit_id`, `product_type_id`, and a
  `attributes JSONB` column for type-specific fields (e.g., for
  textile: `{"gsm": 180, "width_cm": 150, "color": "navy"}`; for a
  hardware dealer: `{"material": "steel", "size_mm": 12}`). The JSONB
  schema itself is declared per `product_type`, not hard-coded.
- Units are a `units` table (KG, PCS, MTR, ROLL, BOX, ...), not an enum
  baked into code. Weight-based costing (weighted average per KG) is
  one strategy the costing engine supports; piece-based and
  length-based costing are the same engine, different unit.
- OCR column mapping lives in `ocr_templates`, keyed by
  `product_type_id` (and optionally `supplier_id` for supplier-specific
  layouts). The textile template that ships on day one encodes exactly
  the "Qty | Description | Code | Label | KG | T.KG" layout from the
  reference sheets, with `S.NO`, `Label`, and `Total` marked as
  ignored columns — see [07_OCR.md](07_OCR.md#templates).
- **What is *not* generalized on day one, and why:** multi-tenancy
  (multiple unrelated businesses sharing one deployment) is *not*
  built now, even though the schema carries an `org_id` on every table
  to make that migration additive later rather than a rewrite. Building
  full multi-tenant isolation, billing, and tenant admin now would be
  designing for a hypothetical SaaS customer who doesn't exist yet,
  at the cost of real complexity the two actual partners would pay for
  today. This tradeoff is recorded, not hidden — see
  [18_FutureRoadmap.md](18_FutureRoadmap.md#multi-tenancy).

## 5. Personas

### Partner (primary user, role = `owner`)
Two individuals, joint owners. Both have full access to every command.
Either partner's WhatsApp number is a fully trusted input source — the
system does not model an approval workflow between the two partners
for day-to-day entries (see [14_Security.md](14_Security.md#rbac) for
why, and for the one thing that *does* require both: capital
withdrawals above a configurable threshold).

Needs: enter a purchase in under a minute from a phone photo; know
today's cash and bank position without opening a spreadsheet; catch
mistakes (their own or their partner's) before they become a
reconciliation headache weeks later.

### Staff (role = `staff`, optional, provisioned later)
A trusted employee who can record sales and purchases but cannot view
partner capital, issue withdrawals, or change settings. See the RBAC
matrix in [14_Security.md](14_Security.md#rbac).

### Accountant / auditor (role = `viewer`, dashboard-only)
Reviews books periodically. Read-only access to the web dashboard and
reports, no WhatsApp access required. Never mutates data.

## 6. Goals

- Every transaction type the partners currently track in Excel/paper
  is representable and enterable from WhatsApp within one command or
  one guided conversation.
- OCR purchase entry matches the reference sample sheets with
  ≥95% field-level accuracy after the learning dictionary has seen 20
  corrections for a given supplier (measured in
  [15_Testing.md](15_Testing.md#ocr-accuracy-benchmarks)).
- The books never silently diverge from physical reality: every
  automatic check in [`CLAUDE.md`](../CLAUDE.md#intelligent-behaviors)
  is active from day one, not added later.
- The system can be operated by someone with no ERP or accounting
  background, using command syntax close to how the partners already
  describe transactions verbally.

## 7. Non-goals (explicitly out of scope for v1)

- Multi-tenant SaaS (see §4 and [18_FutureRoadmap.md](18_FutureRoadmap.md)).
- Multi-currency. All amounts are a single configured currency
  (`settings.base_currency`, defaults to INR). Cross-currency purchases
  are out of scope until a real need exists.
- Payroll / HR.
- Manufacturing / BOM (bill of materials) / production tracking. This
  is a trading (buy-sell) ERP, not a manufacturing one.
- Full GST/tax filing integration. Tax fields are captured
  (see [06_Accounting.md](06_Accounting.md#tax-handling)) but filing
  itself is a manual export, not an automated submission, in v1.
- Native mobile app. WhatsApp *is* the mobile interface.

## 8. Success metrics

| Metric | Target |
|---|---|
| Time to enter a purchase from photo to confirmed save | < 90 seconds median |
| OCR field-level accuracy (textile template, trained) | ≥ 95% |
| Duplicate invoices that reach the ledger undetected | 0 |
| Inventory reconciliation drift (system vs. nightly recompute) | 0, always |
| Partner-reported "I don't trust this number" incidents | trending to 0 after month 1 |
| Dashboard/summary response time over WhatsApp | < 3 seconds |

## 9. Glossary

| Term | Meaning |
|---|---|
| Partner | Business owner with full system access; also the accounting entity capital is tracked against |
| Product | Generic term for what used to be called "item" — anything bought/sold, textile or otherwise |
| Product type | Configuration bundle: unit system, OCR template, custom attribute schema |
| Movement | A single append-only inventory change (purchase in, sale out, return, adjustment) |
| Weighted average cost | Costing method where the recorded unit cost is the quantity-weighted average of all purchases to date, recalculated on every purchase |
| Learning dictionary | Table of OCR misreadings → corrections, keyed by supplier, used to auto-correct future scans |
| Session | A stateful, multi-turn WhatsApp conversation (e.g., confirming an OCR preview) tracked in `whatsapp_sessions` |
