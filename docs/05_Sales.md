# 05 — Sales

## 1. User stories

- *As a partner*, I type one message with a customer name and a list of
  code/qty/rate triples and the sale is recorded, stock reduced, and a
  receivable created if it's on credit — no separate screens.
- *As a partner*, if I quote a price below what we paid for the stock,
  I want to be warned immediately (before I regret it), not find out at
  month-end when profit looks wrong.
- *As a partner*, if I accidentally send the same sale message twice
  (double-tap, WhatsApp resend after a connectivity blip), it must not
  double-count.
- *As a partner*, I can record a partial payment against a credit sale
  as money comes in, and always know who owes us how much.

## 2. Sale command grammar {#grammar}

**Syntax (matches `CLAUDE.md`'s example exactly):**
```
sale Customer: <name> [cash|bank|credit]
<CODE> <qty> <rate>
<CODE> <qty> <rate>
...
```

Payment type defaults to `credit` if omitted (the common case per the
partners' existing workflow — most trade sales are on account).

**BNF:**
```
<sale_command>   ::= "sale" "Customer:" <customer_name> [<payment_type>] <newline> <line>+
<payment_type>   ::= "cash" | "bank" | "credit"
<line>           ::= <product_code> <ws> <qty> <ws> <rate>
<product_code>   ::= /[A-Za-z0-9\-]{1,20}/
<qty>            ::= /[0-9]+(\.[0-9]{1,3})?/
<rate>           ::= /[0-9]+(\.[0-9]{1,2})?/
```

**Example (from CLAUDE.md):**
```
sale Customer: ABC
TRP 20 165
MJP 5 220
```

**Success response:**
```
✅ Sale recorded — ABC (credit)
TRP  20 KG × ₹165.00 = ₹3,300.00
MJP   5 KG × ₹220.00 = ₹1,100.00
Total: ₹4,400.00
ABC now owes: ₹4,400.00 (was ₹0.00)
Stock after: TRP 130.0 KG · MJP 35.0 KG
```

Full error-response catalogue, permission rules, and edge cases for
this command are specified in
[08_WhatsApp.md #sale](08_WhatsApp.md#sale); this document covers the
business logic behind it.

## 3. Stock reduction and receivables

On confirmation (sales under a configurable auto-confirm threshold —
see [08_WhatsApp.md §5](08_WhatsApp.md#session-state-machine) — skip
the explicit CONFIRM step that purchases require, since a mistaken sale
entry is cheaply reversible via `undo` and requiring confirmation on
every single sale line would slow down the highest-frequency command
in the system):

1. For each line: `InventoryService.record_sale_movement` — creates a
   `sale` movement (`qty_delta = -qty`, per
   [03_Inventory.md](03_Inventory.md)), snapshotting
   `sales_lines.avg_cost_at_sale_time = inventory.weighted_avg_cost`
   at that instant (needed for margin reporting and for correct
   sale-return costing).
2. If `payment_type = credit`: no `cash_ledger`/`bank_ledger` entry;
   the customer's outstanding balance (computed as
   `SUM(sales_headers.grand_total - amount_paid)` for that customer,
   plus `opening_balance`) increases by `grand_total`.
3. If `payment_type = cash` or `bank`: a ledger inflow row is created
   in the same transaction, `amount_paid = grand_total`,
   `payment_status = paid` immediately.

## 4. Below-cost sale warning {#below-cost-warning}

Before committing, each line's `rate` is compared to
`inventory.weighted_avg_cost` for that product:

```
if rate < weighted_avg_cost * (1 - tolerance):
    raise BelowCostSaleWarning(product, rate, weighted_avg_cost, margin_percent)
```

`tolerance` defaults to 0 (any sale strictly below average cost warns)
via `settings.below_cost_sale_tolerance_percent`, adjustable if the
partners want headroom for planned clearance sales. This is a
**warning that requires one extra confirmation, not a hard block** —
selling below cost is sometimes a deliberate business decision
(clearing slow-moving stock, a relationship discount), and the system
does not get to override that decision — it only makes sure it's
never *accidental*:

```
⚠️ TRP is being sold at ₹140.00/KG but average cost is ₹153.21/KG
(loss of ₹13.21/KG, −8.6% margin). Reply "confirm" to proceed anyway,
or send a corrected rate.
```

Once confirmed, the sale proceeds and is tagged
(`audit_logs.after_state.below_cost_confirmed = true`) so the weekly
summary can surface "sales below cost this week" as a distinct
reviewable line — visibility, not prevention, is the point.

## 5. Duplicate sale detection {#duplicate-sale-detection}

Unlike purchases (which have a natural unique key — supplier +
invoice number), sales have no supplier-provided identifier to key off
of, so duplicate detection here is about **catching accidental
re-sends**, not catching two genuinely separate sales that happen to
look similar (a customer legitimately buying the same items twice in a
day is normal and must not be blocked).

Two mechanisms:

1. **Idempotency key** (`sales_headers.idempotency_key`): every
   WhatsApp sale message is hashed
   (`sha256(sender_number + normalized_message_text)`) and stored as
   the idempotency key. If the *exact same message text* arrives again
   from the *same sender* within `settings.sale_dedup_window_minutes`
   (default 10), the second one is a no-op:
   ```
   ↩️ This looks identical to the sale you just sent 40 seconds ago
   (ABC, TRP 20 165, MJP 5 220) — not recorded again. If this really
   is a second, separate sale, add anything to the message (e.g. a
   space or note) and resend.
   ```
   This directly implements "detect accidental repeated WhatsApp
   messages" from
   [`CLAUDE.md`](../CLAUDE.md#intelligent-behaviors) — see
   [08_WhatsApp.md #message-deduplication](08_WhatsApp.md#message-deduplication)
   for the transport-level dedup this complements (that one catches
   WhatsApp's own network-retry redelivery of the *same webhook*; this
   one catches the *user* fat-fingering send-twice, a different
   failure mode at a different layer).
2. **Soft near-duplicate warning** (distinct from the hard idempotency
   block above): same customer, same product set, same total, within
   `settings.sale_dedup_window_minutes`, but *not* byte-identical text
   (e.g., the partner retyped it slightly differently). This is a
   warning, not a block, since it's plausible: "This looks similar to
   a sale to ABC you recorded 3 minutes ago (₹4,400.00). Reply
   'confirm' to record it separately, or 'cancel'."

## 6. Sale returns

**Command:** `return sale <sale-id-or-recent-ref> <CODE> <qty>` — full
syntax in [08_WhatsApp.md #return](08_WhatsApp.md#return).

- Validates `returned_qty_so_far + this_qty <= sales_lines.qty`
  (cannot return more than was sold).
- Creates a `sale_return` movement, adding stock back **at
  `sales_lines.avg_cost_at_sale_time`** (the historical cost at time of
  original sale — see [03_Inventory.md §2](03_Inventory.md#2-weighted-average-cost--the-algorithm)),
  not today's average cost, so a return doesn't distort current costing
  based on an old transaction.
- If the original sale was `credit`, the customer's receivable is
  reduced by the returned line's value
  (`qty_returned * original_rate`). If the original sale was
  `cash`/`bank` and already fully paid, the return creates a
  **refund obligation**, not an automatic cash-ledger reversal — the
  system asks: "Refund ₹3,300.00 to ABC now (cash/bank), or record as
  credit against their next purchase?" — because whether physical cash
  actually left the drawer is a fact only the partner knows, and the
  system must never assume a ledger movement that didn't really happen.
- `sales_headers.status` transitions to `returned` (if all lines fully
  returned) or `partially_returned`.

## 7. Partial payments against credit sales

- `received Customer: ABC 2000 cash` ([08_WhatsApp.md #received](08_WhatsApp.md#received))
  applies ₹2,000 against ABC's oldest outstanding sales first (FIFO
  settlement — the standard, predictable allocation order; a partner
  can override which invoice a payment applies to via
  `received Customer: ABC 2000 cash against INV-ref` if needed).
- Each payment updates `sales_headers.amount_paid` and
  `payment_status`, and creates a `cash_ledger`/`bank_ledger` inflow
  row, atomically.
- Overpayment beyond total outstanding is handled the same way as
  purchase overpayment (§9 in [04_Purchases.md](04_Purchases.md#9-payment-tracking)):
  flagged, and recorded as a customer credit balance if confirmed.

## 8. Credit limit enforcement

- `customers.credit_limit` (nullable — unlimited if unset).
- Before confirming a `credit` sale, if
  `current_outstanding + new_sale_total > credit_limit`, the system
  warns rather than blocks (consistent with the below-cost philosophy
  — the system surfaces risk, the partner decides):
  ```
  ⚠️ ABC's credit limit is ₹50,000; this sale would bring their
  outstanding to ₹54,400.00. Reply "confirm" to proceed, or "cancel".
  ```
- `owner` role can proceed past this warning; `staff` role cannot — a
  credit-limit override for staff requires a partner's confirmation
  (same RBAC pattern as duplicate-invoice override, see
  [14_Security.md #rbac](14_Security.md#rbac)).

## 9. Validation rules (exhaustive)

| Field | Rule | Failure behavior |
|---|---|---|
| `customer` | Resolves via exact/fuzzy match (≥0.8) or offered "create new customer?" | Blocks until resolved |
| `payment_type` | One of `cash`/`bank`/`credit`; defaults to `credit` | N/A (has default) |
| product code per line | Resolves via `products.code`; unresolved codes ask individually | Line held pending |
| `qty` | `> 0`; checked against `inventory.qty_on_hand` (see [03_Inventory.md §3](03_Inventory.md#3-negative-stock)) | Blocked by default, `override` available |
| `rate` | `>= 0`; triggers below-cost warning per §4 | Warning + confirm |
| line count | ≥ 1 | Reject empty sale |
| `credit_limit` | See §8 | Warning + confirm (owner) / escalate (staff) |

## 10. Failure scenarios

| Scenario | Behavior |
|---|---|
| Stock insufficient for one line among several | Only the insufficient line blocks; the message identifies exactly which line and lets the partner correct just that quantity rather than re-typing the whole sale. |
| Customer name matches two different existing customers equally well (ambiguous fuzzy match) | Both are shown as numbered options; the partner replies with a number — never auto-picked when ambiguous. |
| Sale confirmed, then `undo`ed, but stock was already partially re-sold in between | `undo` reverses the specific movement it created; if that leaves `qty_on_hand` negative because of the intervening sale, the standard negative-stock flag from [03_Inventory.md §3](03_Inventory.md#3-negative-stock) applies and is surfaced, not hidden. |
| WhatsApp session expires mid-multi-line sale entry (partner sent "sale Customer: ABC" then got interrupted before sending line items) | Session times out per [08_WhatsApp.md §5](08_WhatsApp.md#session-state-machine); no partial sale is ever created — a sale is only written once a complete, valid message (or a completed guided flow) is received. |
| Rate provided as `0` | Treated as a below-cost warning (§4), same as any other below-cost rate — not a special case, since ₹0 is just an extreme discount from the system's point of view. |

## 11. API endpoints

```
GET  /api/v1/sales                 list, filterable by customer, date range, payment_status
GET  /api/v1/sales/{id}
POST /api/v1/sales/{id}/undo       mirrors WhatsApp undo, owner-only for confirmed sales
GET  /api/v1/customers/{id}/ledger full statement: sales, payments, returns, running balance
```

Full shapes in [10_API.md](10_API.md#sales).

## 12. Example JSON — sale detail

```json
{
  "id": "f5a2...",
  "customer": { "id": "a9...", "name": "ABC" },
  "sale_date": "2026-07-24",
  "payment_type": "credit",
  "grand_total": "4400.00",
  "amount_paid": "0.00",
  "payment_status": "unpaid",
  "status": "confirmed",
  "lines": [
    {
      "line_no": 1,
      "product": { "code": "TRP" },
      "qty": "20.000",
      "rate": "165.0000",
      "line_total": "3300.00",
      "avg_cost_at_sale_time": "153.2100",
      "returned_qty": "0.000"
    },
    {
      "line_no": 2,
      "product": { "code": "MJP" },
      "qty": "5.000",
      "rate": "220.0000",
      "line_total": "1100.00",
      "avg_cost_at_sale_time": "214.4900",
      "returned_qty": "0.000"
    }
  ]
}
```

## 13. Sequence diagram: sale with below-cost warning

```mermaid
sequenceDiagram
    participant U as Partner
    participant API as FastAPI webhook
    participant SVC as SalesService
    participant INV as InventoryService
    participant DB as PostgreSQL

    U->>API: "sale Customer: ABC\nTRP 20 140"
    API->>SVC: handle_sale_command(text, sender)
    SVC->>DB: resolve customer, product
    SVC->>INV: check_stock(TRP, 20)
    INV-->>SVC: sufficient
    SVC->>SVC: compare rate(140) vs avg_cost(153.21) -> below cost
    SVC-->>API: BelowCostSaleWarning
    API->>U: "⚠️ below average cost... reply confirm"
    U->>API: "confirm"
    API->>SVC: resume with confirmation flag
    SVC->>DB: BEGIN
    SVC->>INV: record_sale_movement(line)
    INV->>DB: SELECT inventory FOR UPDATE; INSERT movement; UPDATE inventory
    SVC->>DB: INSERT sales_headers/sales_lines
    SVC->>DB: INSERT audit_logs (below_cost_confirmed=true)
    SVC->>DB: COMMIT
    SVC-->>API: success summary
    API->>U: "✅ Sale recorded..."
```
