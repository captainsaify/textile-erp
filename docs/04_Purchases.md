# 04 — Purchases

## 1. User stories

- *As a partner*, I photograph a supplier's purchase sheet and get a
  parsed, editable preview within seconds, so I don't retype what's
  already printed on the invoice.
- *As a partner*, when I confirm a purchase, I want to be warned if it
  looks like I already entered this exact invoice, so I never
  double-count payables or inventory.
- *As a partner*, I want freight and other charges spread across the
  line items automatically, so each product's landed cost is accurate
  without me doing the division by hand.
- *As a partner*, if the invoice total printed on the sheet doesn't
  match what the line items add up to, I want to know before I confirm,
  not after I've already paid the supplier based on a wrong number.
- *As staff*, I can enter a purchase but cannot alter freight allocation
  method or override a duplicate-invoice warning without an `owner`
  confirming (see [14_Security.md #rbac](14_Security.md#rbac)).

## 2. Purchase entry flow (OCR-first, manual fallback)

```mermaid
flowchart TD
    A[Photo/PDF received] --> B[OCR pipeline: 07_OCR.md]
    B --> C{Table detected\nwith usable confidence?}
    C -- No --> D[Fall back to manual entry:\n"Couldn't read this clearly,\nlet's enter it manually"]
    C -- Yes --> E[Draft purchase_headers + purchase_lines\nstatus=draft]
    E --> F[Ask for required_manual_fields\nnot present on the sheet:\nSupplier, Brand, Invoice No.,\nDate, Rate, Freight, Other charges]
    D --> F
    F --> G[Render preview table over WhatsApp]
    G --> H{User replies}
    H -- CONFIRM --> I[Run validation + duplicate detection]
    H -- correction text --> J[Apply correction to draft line,\nupdate ocr_learning_dictionary]
    J --> G
    I -- total mismatch / duplicate found --> K[Explain finding,\nask explicit override or fix]
    K --> H
    I -- clean --> L[Confirm: create inventory_movements,\nupdate weighted avg cost, audit log]
    L --> M[Send final summary + store attachment]
```

## 3. Manual purchase command (fallback / non-OCR entry)

**Syntax:**
```
purchase Supplier: <name> Invoice: <no> Date: <DD-MM-YYYY>
<CODE> <qty> <rate>
<CODE> <qty> <rate>
Freight: <amount>
Other: <amount>
```

**Example:**
```
purchase Supplier: Shree Textiles Invoice: INV-4521 Date: 24-07-2026
TRP 100 150
MJP 40 210
Freight: 500
Other: 100
```

**Success response:**
```
✅ Purchase draft ready — Shree Textiles, INV-4521, 24-07-2026
TRP   100 KG × ₹150.00 = ₹15,000.00
MJP    40 KG × ₹210.00 = ₹8,400.00
Subtotal: ₹23,400.00
Freight: ₹500.00 (allocated by weight)
Other charges: ₹100.00
Grand total: ₹24,000.00
Reply CONFIRM to save, or send corrections.
```

Full command grammar, error cases, and permission rules are specified
alongside every other WhatsApp command in
[08_WhatsApp.md #purchase](08_WhatsApp.md#purchase).

### One instruction at a time {#one-instruction}

A draft can be blocked on several things at once — an unknown supplier,
unknown codes, a brand collision. It asks about **exactly one**.

A real sheet produced, in a single message: `reply 'create supplier'`,
`reply *create all products*`, and `then reply CONFIRM to save`. Three
instructions, of which only one would work, and nothing saying which.
Two of them silently failed if followed.

`purchase_commands.next_step(draft)` returns that one thing, and both
the reply text and the buttons branch on it — so the words above the
buttons can never ask for something different from the buttons. The
order is by what blocks what:

| Step | Asked when | Offered |
|---|---|---|
| `details` | no supplier name or invoice number | nothing yet — the wizard is still collecting |
| `brand` | codes collide with another brand's | `Yes, separate` · `Fix the brand` · `Discard` |
| `codes` | codes aren't in the catalogue | `Create all N` · `One by one` · `Discard` |
| `supplier` | the supplier isn't on file | `Add supplier` · `Discard` |
| `confirm` | nothing is blocking | `Confirm` · `See as sheet` · `Discard` |

A blocker that is *not* the current step is still **stated**, as a fact
rather than an instruction: "Supplier 'Iqbal Bhai' isn't in your list
yet — I'll ask about that next." Hiding it would make the next question
arrive as a surprise; phrasing it as a command would be the bug again.

Nothing says "then reply CONFIRM" ahead of time. The bill comes back
after each step and says for itself what is left.

## 4. Freight and other-charge allocation {#freight-allocation}

`purchase_headers.freight_allocation_method` (default `by_weight`):

| Method | Formula per line | When used |
|---|---|---|
| `by_weight` | `freight * (line.total_weight_kg / sum(all lines total_weight_kg))` | Default for weight-costed product types (textile) — matches how freight is actually billed (per-KG transport). |
| `by_value` | `freight * (line.line_total / subtotal)` | Product types not costed by weight, or when the partner explicitly says "split by value." |
| `by_qty` | `freight * (line.qty / sum(all lines qty))` | Rare — uniform per-unit freight regardless of weight/value. |
| `manual` | Partner supplies a freight amount per line directly | Escape hatch when the automatic split doesn't match reality (e.g., one item was collected separately). |

`other_charges` (loading, unloading, local transport, etc.) always
allocates `by_value` regardless of the freight method, since these
charges are typically proportional to goods value, not weight — this
is a documented default, overridable the same way as freight via
`manual`.

Allocated amounts are stored per line
(`purchase_lines.freight_allocated`) rather than only at the header
level, because landed cost (§ below) needs a per-line figure, and
recomputing the split every time it's needed (rather than storing it)
risks drift if the allocation method changes after the fact — it
doesn't; once a purchase is confirmed, `freight_allocation_method` is
immutable for that purchase (a correction requires `undo` + re-entry,
not an in-place edit, exactly because changing it after inventory
movements exist would desynchronize the stored allocation from the
movements already posted).

**Rounding**: allocation splits are computed in `Decimal`, and any
rounding remainder (paise left over after splitting ₹500 across three
lines) is added to the **last line by value** — a simple, deterministic
rule that means `sum(line.freight_allocated) == header.freight` always
holds exactly, which is asserted in a unit test
([15_Testing.md](15_Testing.md)).

**Landed cost per unit** (feeds weighted average, see
[03_Inventory.md §2](03_Inventory.md#2-weighted-average-cost--the-algorithm)):
```
landed_cost_per_unit = (line_total + freight_allocated + other_charges_allocated) / qty
```

## 5. Total mismatch detection {#total-mismatch}

Every OCR-parsed or manually entered purchase captures
`declared_total` (the total printed on the supplier's sheet, when
legible) separately from the system-computed `grand_total`
(`subtotal + freight + other_charges`). Before confirmation:

```
if declared_total is not None and abs(declared_total - grand_total) > tolerance:
    raise TotalMismatchWarning(declared_total, grand_total, difference)
```

`tolerance` defaults to ₹1.00 (rounding slack), configurable via
`settings.purchase_total_mismatch_tolerance`. This is a **warning, not
a hard block** — the system shows both numbers and asks the partner to
confirm which is right, because the mismatch is sometimes the OCR
misreading a digit and sometimes the supplier's own arithmetic error:

```
⚠️ The invoice shows a total of ₹24,150.00, but the line items +
freight + other charges add up to ₹24,000.00 (difference: ₹150.00).
Reply "use invoice total", "use calculated total", or correct a line.
```

If the partner picks "use invoice total," the difference is recorded
as an `other_charges` adjustment with `notes = 'reconciled against
declared invoice total'` rather than silently overriding
`grand_total` with no trace — the audit log always shows how the
final number was reached.

## 6. Duplicate invoice detection {#duplicate-detection}

Two layers, because exact-match alone misses real duplicates (retyped
invoice number with a typo, re-scanned photo of the same sheet) and
fuzzy-match alone would be too aggressive to run as a hard block.

**Layer 1 — exact, hard block (schema-enforced):**
`UNIQUE (org_id, supplier_id, invoice_no)` on `purchase_headers` (only
enforced for non-deleted rows, via partial index per
[02_Database.md §4](02_Database.md#soft-delete)). A second purchase
with the identical supplier + invoice number cannot be confirmed —
this raises `ExactDuplicateInvoiceError`, always a hard stop:
```
❌ Invoice INV-4521 from Shree Textiles is already recorded
(confirmed 24-07-2026, ₹24,000.00). This wasn't saved.
```

**Layer 2 — fuzzy, soft warning, application-level:**
Runs at confirmation time (see
[01_Architecture.md §12](01_Architecture.md#12-illustrative-pattern-not-a-stub-this-is-the-actual-shape-every-servicerepository-follows)
for the actual code shape). Candidates are pulled via
`find_potential_duplicates`: same `supplier_id`, `invoice_date` within
±3 days (`settings.duplicate_invoice_window_days`, default 3) of the
new purchase. A candidate is flagged as a probable duplicate if **at
least two** of the following hold (deliberately requiring 2-of-3 so a
single coincidence — e.g., same total on an unrelated invoice — doesn't
false-positive):
1. `invoice_no` similarity ≥ 0.85 (Levenshtein ratio via `rapidfuzz`,
   handles OCR/typo variants like `INV-4521` vs `INV-4521.` vs
   `1NV-4521`).
2. `grand_total` within 1% of the candidate's `grand_total`.
3. Line-item set overlap: ≥ 70% of `product_id`s match between the two
   purchases' lines, by count.

```
⚠️ This looks similar to a purchase already recorded:
Shree Textiles, INV-4521. (confirmed 22-07-2026, ₹23,950.00 — 92% match)
Reply "confirm anyway" or "cancel".
```

An `owner`-only override (`confirm anyway`) is required — `staff` role
cannot dismiss a fuzzy-duplicate warning (see
[14_Security.md #rbac](14_Security.md#rbac)); the WhatsApp bot tells
staff to forward the warning to a partner.

**Photo-level duplicate detection** (belt and suspenders): every
uploaded attachment's `sha256_hash` (§3.19 in
[02_Database.md](02_Database.md)) is checked against existing
attachments for the org before OCR even runs — an identical photo sent
twice (e.g., accidentally forwarded) is caught immediately:
```
📎 You already sent this exact photo on 24-07-2026 (linked to purchase
INV-4521). Send a different photo, or reply "process anyway" if this
is intentional (e.g., a second copy of the same invoice you actually
need entered twice for some reason).
```

## 7. Validation rules (exhaustive)

| Field | Rule | Failure behavior |
|---|---|---|
| `supplier` | Must resolve to an existing `suppliers` row (fuzzy-matched, ≥0.8 similarity) or the user is offered "create new supplier?" | Blocks confirmation until resolved |
| `invoice_no` | Non-empty, max 100 chars | Reject, ask again |
| `invoice_date` | Valid date, not in the future beyond today (business's local date), not more than 2 years in the past without explicit `owner` override | Reject / warn |
| `product code` per line | Must resolve via `products.code` exact or fuzzy match (≥0.85) or learning dictionary; unresolved codes are asked about individually, never silently dropped | Line held in draft as `unresolved` until answered |
| `qty` | `> 0`, ≤ 100,000 (sanity ceiling, configurable) — absurd values (e.g., OCR reading "1000" for "100") are flagged for confirmation, not blocked outright | Soft warning: "100000 KG of TRP — that's unusually high, please confirm" |
| `rate` | `>= 0`; a rate of `0` triggers a warning ("free goods?") rather than a block, since free/promotional stock is plausible | Soft warning |
| `freight`, `other_charges` | `>= 0` | Reject negative |
| line count | At least 1 line required to confirm | Reject empty purchase |

## 8. Edit, undo, delete

- **Edit** (`edit purchase INV-4521 ...`): only permitted while
  `status = draft`. Once `confirmed`, a purchase is immutable — no
  in-place edits to a confirmed purchase, ever, because inventory
  movements and weighted-average cost have already been derived from
  it (see [03_Inventory.md](03_Inventory.md)). A correction to a
  confirmed purchase is `undo` (reverses it) followed by re-entry.
- **Undo** (`undo` / `undo purchase INV-4521`): available within
  `settings.undo_window_hours` (default 24h) of confirmation, `owner`
  only for purchases already confirmed (staff can undo their own
  *draft* actions freely). Implemented as compensating entries, never
  row deletion:
  - `purchase_headers.status -> cancelled` (not soft-deleted — the
    record and the fact that it was cancelled both stay visible).
  - One `purchase_return`-equivalent reversal movement per original
    `purchase` movement, fully reversing qty and reverting
    `weighted_avg_cost` via the same exact-reversal math described in
    [03_Inventory.md §4](03_Inventory.md#4-returns-damage-and-adjustments)
    (undo is always an exact reversal since it happens immediately
    after, before other purchases of the same product could have
    intervened in the common case — if they have, the same
    approximation-and-flag fallback in §4 applies).
  - `audit_logs` row for the undo itself, distinct from the original
    confirm.
- **Delete**: not exposed as a separate operation from `undo` for
  confirmed purchases — deleting a financial record outright is never
  allowed; "delete" in the WhatsApp `delete` command
  ([08_WhatsApp.md](08_WhatsApp.md#delete)) on a purchase is routed to
  the same undo/cancel flow. A `draft` purchase (never confirmed, e.g.
  an abandoned OCR session) can be hard-removed via soft delete since
  no financial movement was ever derived from it.

## 9. Payment tracking

- `payment_status` derives from `amount_paid` vs `grand_total`
  (`unpaid` / `partial` / `paid`), recomputed whenever a `paid`
  WhatsApp command ([08_WhatsApp.md #paid](08_WhatsApp.md#paid))
  applies a payment against this purchase.
- A `paid` entry creates a `cash_ledger` or `bank_ledger` outflow row
  and updates `purchase_headers.amount_paid` in the same transaction.
- **Partial payments** are fully supported: multiple `paid` entries
  against the same invoice accumulate; the outstanding balance
  (`grand_total - amount_paid`) is what `supplier NAME`
  ([08_WhatsApp.md](08_WhatsApp.md#supplier-name)) reports as payable.
- Overpayment (`amount_paid > grand_total`) is not silently clamped —
  it's flagged: "This payment would make total paid (₹24,500) exceed
  the invoice total (₹24,000) by ₹500. Confirm, or is this an advance
  against a future invoice?" — and if confirmed, the excess is
  recorded as a supplier advance credit, tracked in the supplier's
  running balance rather than lost.

## 10. Failure scenarios

| Scenario | Behavior |
|---|---|
| WhatsApp media download fails (network blip) | Celery task retries with backoff (see [11_BackgroundWorkers.md](11_BackgroundWorkers.md#retry-policy)); after 3 failures, user is told "Couldn't download your photo, please resend." |
| OCR pipeline crashes on a malformed image | Caught, attachment marked `status=failed`, user offered manual entry immediately rather than left waiting silently. |
| Two purchases for the same invoice confirmed concurrently by both partners (race) | The `UNIQUE (org_id, supplier_id, invoice_no)` constraint (partial index, active rows) makes the second `INSERT`/status-flip fail at the DB level regardless of application-level timing; the second confirmer gets the exact-duplicate message from §6 layer 1, not a 500 error — the service catches the `IntegrityError` and translates it. |
| Product code in a purchase line doesn't exist and fuzzy match finds nothing | User is asked "Create new product 'XYZ'?" — never auto-created silently, since a garbled OCR code becoming a permanent phantom product is worse than asking once. |
| Confirmation reply is ambiguous ("looks fine" instead of "CONFIRM") | Session state machine ([08_WhatsApp.md §5](08_WhatsApp.md#session-state-machine)) only accepts a small recognized vocabulary for confirmation (`confirm`, `yes`, `ok`, `save` — configurable); anything else is treated as a correction attempt and re-parsed, with a nudge: "Reply CONFIRM to save this purchase, or tell me what to fix." |
| Draft purchase abandoned (no reply for 30+ min) | Session expires (§ [08_WhatsApp.md](08_WhatsApp.md#session-state-machine)); the draft `purchase_headers` row remains (never auto-deleted — it's recoverable via `edit`), and a gentle nudge is sent once: "You have an unconfirmed purchase from Shree Textiles — reply to finish it, or 'discard'." |

## 11. API endpoints (admin/dashboard surface — mutating actions still require WhatsApp for the create/OCR flow; see rationale in [10_API.md](10_API.md))

```
GET    /api/v1/purchases                 list, filterable by supplier, date range, status
GET    /api/v1/purchases/{id}            full detail incl. lines
POST   /api/v1/purchases/{id}/undo       owner-only, mirrors WhatsApp undo
GET    /api/v1/purchases/{id}/attachment redirect/stream the original scanned invoice
```

Full request/response shapes in [10_API.md](10_API.md#purchases).

## 12. Example JSON — confirmed purchase detail

```json
{
  "id": "6a1e1e4e-2222-4a3a-9c33-111111111111",
  "supplier": { "id": "b1...", "name": "Shree Textiles" },
  "brand": { "id": "c2...", "name": "Wagdia" },
  "invoice_no": "INV-4521",
  "invoice_date": "2026-07-24",
  "status": "confirmed",
  "freight_allocation_method": "by_weight",
  "subtotal": "23400.00",
  "freight": "500.00",
  "other_charges": "100.00",
  "grand_total": "24000.00",
  "declared_total": "24000.00",
  "payment_status": "partial",
  "amount_paid": "10000.00",
  "lines": [
    {
      "line_no": 1,
      "product": { "id": "d3...", "code": "TRP", "description": "Trouser Poly" },
      "qty": "100.000",
      "weight_kg": "1.000",
      "total_weight_kg": "100.000",
      "rate": "150.0000",
      "line_total": "15000.00",
      "freight_allocated": "320.51",
      "landed_cost_per_unit": "153.21",
      "ocr_confidence": "0.940"
    },
    {
      "line_no": 2,
      "product": { "id": "e4...", "code": "MJP", "description": "Micro Jogging Pants Fabric" },
      "qty": "40.000",
      "weight_kg": "1.000",
      "total_weight_kg": "40.000",
      "rate": "210.0000",
      "line_total": "8400.00",
      "freight_allocated": "179.49",
      "landed_cost_per_unit": "214.49",
      "ocr_confidence": "0.910"
    }
  ]
}
```
