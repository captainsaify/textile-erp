# 29. One person on both sides of the book

Afzal sells us fabric. Afzal also buys fabric from us. A purchase must
name a supplier and a sale must name a customer, so he is necessarily
two rows in two tables — and until those rows were declared to be one
man, the books held a payable of ₹5,00,000 and a receivable of ₹98,000
as unrelated balances. Two statements. Two numbers. The one number
either partner actually asks about him — *where do we stand with
Afzal* — existed nowhere in the system.

This is not an accident of the schema, it is the schema working
correctly. The fix is not to merge `suppliers` and `customers`; that
would rewrite every foreign key in the database to buy a property one
nullable column provides.

## 1. Linking

`suppliers.linked_customer_id` says the two rows are one person.
Nullable, because trading both ways is the exception. Unique, because a
supplier is at most one customer — a many-to-many here would be a data
model for a question nobody asked.

Linking moves no money. It makes two things possible that were not
before.

## 2. The net position

`PartyLinkService.position()` returns the payable, the receivable, and
the net. Positive net means we owe them; negative means they owe us.

The receivables and payables lists are netted through
`net_against_links()`. A linked party appears **once**, on whichever
side they are actually on, and drops off both lists when the two sides
cancel exactly. Before this, one person appeared on both lists at full
value, which read as more money in play than there was.

Both lists carry `gross` and `offset` beside the netted `outstanding`.
"We owe Afzal ₹4,02,000" and "we owe Afzal ₹5,00,000, less the ₹98,000
he owes us" are answers to different questions, and a screen that shows
only the net invites the reconciliation call that starts *but your own
bill says*.

## 3. The combined statement

A linked party's statement is one timeline covering both sides. The
other side's rows are flipped, because the statement has one balance
column and it can only mean one thing. Read down a supplier's combined
statement, the balance always means **what we owe him, net**:

| Event | Effect on the balance |
|---|---|
| His bill to us | raises it |
| Our payment to him | lowers it |
| Our sale to him | lowers it |
| His payment to us | raises it |

The opening balance for a period is merged the same way, so a statement
that starts mid-year opens on the real net rather than on one side of
it.

## 4. Setting off

Set-off clears the smaller side against the larger, invoice by invoice,
oldest first — the same rule a payment follows. It posts a contra
journal entry:

```
DR  accounts_payable      amount
CR  accounts_receivable   amount
```

**No cash ledger row is written, because no cash moved.** That is
precisely why the journal entry is required: a payable that shrank with
no payment behind it is otherwise unexplainable at year end. Recording
a set-off as a payment would put money through a drawer nobody opened,
and the cash reconciliation would fail that night.

`amount` defaults to the whole overlap — the usual case, since a
set-off that leaves a sliver behind is a set-off someone has to
remember to finish. An explicit amount above the overlap is refused,
naming both sides.

### Why it is explicit

Set-off does **not** happen quietly inside `pay` or `received`. Netting
a receivable against a payable is an agreement between two businesses,
not an arithmetic convenience, and the day it is agreed is the day it
belongs in the books. Someone has to say so, and the audit log records
who did and when (`party.setoff`, with every invoice it touched).

Set-off is refused entirely on an unlinked supplier. Guessing from a
matching spelling is how two different Afzals become one.

## 5. Where it lives

| Piece | Where |
|---|---|
| Link, unlink, position, set-off | `backend/services/party_link_service.py` |
| Netting the money lists | `net_against_links()`, same file |
| Combined statement | `ReportService._merge_sides()` |
| API | `POST/GET/DELETE /control/links`, `POST /control/setoff` |
| Screen | Master Control → Money → "Buys and sells" |
