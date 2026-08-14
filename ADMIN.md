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
brands.

**Fix whichever one is actually wrong — the bill or the sale — not
both.** If you bought `55X AR` correctly and then *sold* it under the
wrong code, the bill is right and only the sale needs moving; the
purchase stays where it is and `55X AR` goes back to holding the stock
you still have. The two are only linked by one thing: stock cannot go
below zero. If the item you are moving *to* does not have enough on
hand, the command rolls back and says which product and by how much.

### …fix a code that was read wrongly

*The TRP under MKD is 003P, not 003B.*

```bash
erp fix purchase 007 --line 2 --code 003P
erp fix sale 12 --line 1 --code 003P --brand MKD
```

### …fix an item that was sold under the wrong code

*You have `55X AR`, but the sale went out as `55X AR` when it was
really `003B`.*

```bash
erp show stock 003B                    # which brand has enough?
erp --dry-run fix sale <ref> --line 2 --code 003B --brand MKD
```

The purchase bill is not involved. Check the stock first — moving a
sale of 800 onto a product holding 560 is the one thing that will be
refused.

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

### …combine two products that are one product

```bash
erp products                            # the catalogue, with what has happened to each
erp products 55X                        # just the ones matching that
erp merge product 55X into 55XL --to-label TOP
erp delete-product 55XL --label TOP     # only if it has never been bought or sold
```

Now that codes can be created while typing a bill, duplicates are a
matter of when rather than if — `55X` under `TOP` and `TOP ` is two
products holding half the stock each.

Merging products is not like merging parties. Merging a party moves who
owes what and no number changes. Merging products moves **stock
movements**, so the survivor's average cost is recomputed over both
histories in order:

```
55X   10 kg at ₹100  =  ₹1,000
55XL  10 kg at ₹200  =  ₹2,000
                        ────────
after 20 kg at ₹150     ₹3,000
```

That is the only value it could honestly have afterwards, and it will
not be either of the two you started with. `erp products` before and
after shows you the move.

`delete-product` is deliberately narrow: it only removes a product
nothing has ever happened to. A product with history is part of the
record, and hiding it would take its purchases out of the reports that
explain the cost of everything else. The error says so, and points at
merge.

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

### …fix a partner whose messages do nothing

```bash
erp contacts                          # who the system can reach, and when it last heard from them
erp relink 7000087329 to "Firoz"      # his new SIM
erp unlink "Firoz"                    # a number that is gone rather than moved
```

A WhatsApp number **is** the login. A message from a number that is not
on that list gets no reply at all — deliberately, so strangers learn
nothing — which means a partner on a new SIM sees nothing happen and has
nothing to report but "it's not working".

`erp contacts` is the diagnosis: a number with **never** in the last
column has either never been used or is wrong, and from a list of names
those look identical.

### …see whether messages are actually arriving

```bash
erp messages                # last 30, with the failures called out
erp messages --failed       # only what did not arrive
erp messages --hours 72     # widen the summary window
```

Meta accepting a message and the partner receiving it are different
facts, and the gap between them used to be visible only in the
container's log. Seventeen failed in one night with code `131047`
— "more than 24 hours since that person last messaged us" — and nobody
knew until someone went looking.

Failures are grouped by cause, because seventeen failures with one cause
are one problem.

### …check nothing is broken

```bash
erp check
```

Reconciles stock against movements and every ledger against the journal,
on both the real books and the demo. Run it after anything unusual. It
is also run automatically inside every command before that command is
allowed to commit.

```bash
erp health                    # size, disk, are the nightly jobs still running
erp rebuild-ledger            # rewrite the running balances from the amounts
```

`erp check` answers whether the books balance. `erp health` answers the
questions underneath it — how big the database has got, how much disk is
left, when the nightly reconciliation last ran (a job that quietly stops
running is the failure nobody notices), and whether each ledger's
running-balance column still equals the sum it claims to summarise.

`rebuild-ledger` is the repair for that last one. Like `recost`, it
computes rather than destroys: the amounts are never touched, only the
derived total beside each one.

### …take a backup

```bash
erp backup
erp backups                   # what can be restored
```

One is taken automatically before every command that changes anything,
and the Master Control screen has a button for it.

### …go back to a backup and carry on from there

```bash
scripts/restore.sh                      # list what you can go back to
scripts/restore.sh backup-2026....dump  # go back to that one
```

**Not from the web app, and not from `erp restore` on its own.** A
restore replaces every table, so Postgres has to lock every table — and
while the API and the workers are connected it cannot, so it waits. A
waiting exclusive lock makes every later query wait behind it too, so
the site goes down and stays down without the restore ever running. That
happened once, from a button that has since been removed.

`scripts/restore.sh` is the whole sequence in one command:

1. backs up the **current** state first, so going back is itself
   reversible — it prints that name at the end;
2. stops the API, the workers and the scheduler;
3. waits for the connections to actually drain, and refuses if they do
   not;
4. restores;
5. starts everything and runs `erp check`.

If it stops at step 3 or 4 it leaves the application **down** on purpose,
rather than starting it on a half-restored database.

`erp restore <name>` still exists and still works, but it now refuses
outright if anything else is connected, and tells you what to stop. A
clear "no" is worth more here than any amount of care taken afterwards,
because a hang is indistinguishable from slowness and the instinct is to
wait.

---

## When it says no

| It says | It means |
|---|---|
| `a payment is allocated against this bill` | Purge or re-point the payment first — it names it |
| `2 sales already went out against MKD 55X` | Changing the brand would send stock negative; fix the sales too, in the same command or before it |
| `ROLLED BACK — nothing was changed` | The change would have unbalanced the books. Nothing happened. Send me the output. |
| `no such code under that brand` | The code exists, but not for that brand. `erp show stock <code>` lists which brands carry it. |
| `55X exists under 3 labels (…) — name one` | Pass `--label`, or `--from-label` / `--to-label` on a merge. Never guessed for you: a merge on the wrong product is reversible, but it should not need to be. |
| `cannot be deleted: it has 4 stock movement(s)` | It has history, so it is part of the record. Merge it into the real product instead. |
| `that number is X's only way to sign in` | Give them an email first. The database refuses a user who can neither text nor log in. |

---

## The one thing to remember

Every command checks that stock and ledgers still balance **before** it
saves, and throws the whole thing away if they do not. So the worst
realistic outcome of a wrong command is that nothing happens.

`purge` is the one to slow down on — not because it cannot be undone
(it can, with `erp restore-purged`) but because a bill quietly missing
from the books is harder to notice than one that is visibly wrong.
