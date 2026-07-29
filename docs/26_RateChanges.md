# 26 — Rate Corrections

## 1. The situation {#situation}

The bill says 150. It should have said 145.

Different from a short delivery ([23](23_ReceiptCorrections.md)): the
quantity was right, so nothing moves. What changes is the price — and
therefore the bill, the payable, and what the stock on hand *cost*.

```
rate 001 145            every line on the bill
rate 001 145 35A 22D    only those codes
```

Several codes at once, because a supplier who revises one price usually
revises a few.

## 2. What moves {#cascade}

| | |
|---|---|
| Purchase line | `rate`, `line_total` |
| Invoice | `subtotal`, `grand_total` |
| Freight | split unchanged — it goes by weight, and no weight moved |
| Other charges | re-split — they go by line value, which did move |
| Landed cost | recomputed per line |
| Stock on hand | revalued at the new landed cost |
| Books | one compensating journal entry |
| Payable | falls out of `grand_total − amount_paid` |

## 3. Revaluing without moving anything {#revaluation}

`InventoryService.restate_cost` posts a movement with **`qty_delta` of
zero**: the goods are still there, they just cost something else.

That keeps `qty_on_hand` equal to the signed sum of its movements —
`CLAUDE.md`'s standing acceptance criterion — while the value changes.
A test asserts it.

The new average is floored at zero. A correction large enough to drive a
cost basis negative means something else is wrong, and inventing one
would silently poison the margin on every later sale.

## 4. What it will not do {#non-goals}

**Goods already sold keep the cost they were sold at.** Their cost went
into COGS when they were sold. Reaching back through every later sale to
re-derive margin is a different operation with a much larger blast
radius, and doing it silently would rewrite profit figures the partners
have already read and acted on.

So only the quantity still on hand is revalued, and any code that is
partly or wholly sold is **named in the reply**:

> ℹ️ Some of 35A has already been sold. The stock still on hand was
> revalued; what was sold keeps the cost it was sold at.

The same two warnings as a receipt correction also apply: correcting
below what has already been paid leaves an advance with the supplier,
and that is said rather than left to be discovered.
