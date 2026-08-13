# Fixing the books yourself

> The sheet to keep open. Design and reasoning live in
> [`docs/31_AdminCLI.md`](docs/31_AdminCLI.md).
>
> **Status: all of it works.** Run `erp --help` on the box for the
> current list.

## Getting in

```bash
ssh textile-erp        # from the Mac
erp show purchase 007  # works from any directory
```

Everything runs on the real books unless you add `--demo`. The prompt
tells you which.

**Two habits worth having:**

- Put `--dry-run` on the end first. It shows exactly what would change,
  including whether the stock still balances, and changes nothing.
- If a command refuses, read what it says before forcing it. It refuses
  when something else depends on what you are about to change, and it
  names the thing.

---

## "I need to…"

### …see what a bill actually says

```bash
erp show purchase 007            # lines, charges, supplier, totals
erp show sale 12
erp show stock 55X               # every brand carrying that code
erp show party "Asif Panipat"    # bills, sales, what is outstanding
erp history 007                  # every change ever made to it, and by whom
```

### …fix the wrong brand on a bill

*Bill 002 was labelled MKD; it was actually LALA.*

```bash
erp fix purchase 002 --line 3 --brand LALA
```

Moves the stock with it and recomputes the average cost on **both**
brands. If sales already went out against the wrong brand, it says so
and names them rather than leaving one side negative.

### …fix a code that was read wrongly

*The TRP under MKD is 003P, not 003B.*

```bash
erp fix purchase 007 --line 2 --code 003P
erp fix sale 12 --line 1 --code 003P --brand MKD
```

### …move a sale to the right customer

*It registered under Rais bhai; it was Sohail bhai.*

```bash
erp fix sale 12 --customer "Sohail Bhai Lucknow"
```

The sale and its unpaid balance move with it: the receivable is derived
from the sale's customer, so both parties' outstanding figures correct
themselves immediately.

One thing it does **not** move — a receipt already banked against the
old party keeps its cash-ledger and journal entries there. If money had
already come in under the wrong name, reverse the receipt first, then
re-record it against the right one.

### …add GST or packing to a bill already confirmed

```bash
erp charge purchase 007 GST 1200
erp charge sale 12 packing 1100 --note "shared with Sohail bhai"
```

Goes into the cost of the goods on a purchase, and into other income on
a sale — not into expenses.

### …correct the price after the fact

```bash
erp fix purchase 007 --rate 107            # the bill
```

### …enter a bill by hand

```bash
erp add purchase \
  --supplier "SHAHNAWAZ TEXTILE" --invoice 009 --date 2026-08-01 \
  --line "55X:90:107:BSQ:zipper sweater" \
  --line "44D:60:121:MKD:sports pant b grade" \
  --charge "GST:1200"
```

Format is `CODE:QTY:RATE:BRAND:DESCRIPTION` — brand and description are
optional. `--line` and `--charge` both repeat. `erp add sale` is the
same shape with `--customer` and no invoice number.

Products must already exist under the brand you name. That is on
purpose: silently creating one here is how a typo ends up as a second
product holding half your stock.

### …combine two parties that are the same person

```bash
erp merge supplier "Yakub Asif" into "Asif Panipat"
erp merge customer "Shahid Bhai Dimapur" into "Zahid Bhai Dimapur"
erp merge brand "TOP " into "TOP"
```

Everything moves to the name on the right. The one on the left stops
existing.

### …combine two bills that are one bill

```bash
erp merge purchase 007B into 007
```

Lines join, charges add up, freight re-spreads across all of them.

### …delete something completely

```bash
erp undo <id>                    # the gentle one: reverses it, keeps the record
erp purge purchase 1051          # out of the books entirely
erp restore-purged purchase 1051 # ...and back, if it was the wrong one
```

`purge` removes the bill from every report, total, ledger, search and
reconciliation — as far as your books are concerned it never happened.
The row is kept hidden so a purge aimed at the wrong invoice is one
mistake instead of two. It makes you type `1051` back first, and takes a
backup either way.

Use `undo` when the bill was real and got cancelled. Use `purge` when it
should never have been in there.

### …correct the stock itself

```bash
erp stock adjust 55X -5 --reason damaged --note "water damage in transit"
erp stock recost 55X          # rebuild average cost from history
erp stock recost --all
```

`recost` is the repair for "the average cost looks wrong" — it replays
every movement including rate corrections.

### …check nothing is broken

```bash
erp check
```

Reconciles stock against movements and every ledger against the journal,
on both the real books and the demo. Run it after anything unusual. It
is also run automatically inside every command before that command is
allowed to commit.

### …take or restore a backup

```bash
erp backup
erp backups                   # what can be restored
erp restore <name>
```

One is taken automatically before every command that changes anything.

---

## When it says no

| It says | It means |
|---|---|
| `a payment is allocated against this bill` | Purge or re-point the payment first — it names it |
| `2 sales already went out against MKD 55X` | Changing the brand would send stock negative; fix the sales too, in the same command or before it |
| `ROLLED BACK — nothing was changed` | The change would have unbalanced the books. Nothing happened. Send me the output. |
| `no such code under that brand` | The code exists, but not for that brand. `erp show stock <code>` lists which brands carry it. |

---

## The one thing to remember

Every command checks that stock and ledgers still balance **before** it
saves, and throws the whole thing away if they do not. So the worst
realistic outcome of a wrong command is that nothing happens.

`purge` is the one to slow down on — not because it cannot be undone
(it can, with `erp restore-purged`) but because a bill quietly missing
from the books is harder to notice than one that is visibly wrong.
