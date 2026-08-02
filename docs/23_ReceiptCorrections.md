# 23 — Receipt Corrections

## 1. The situation {#situation}

The supplier bills 10 bales. Nine arrive.

The invoice was wrong, so the invoice is what gets corrected: the line
reads 9, the bill total falls, and the payable falls with it. Everything
that depends on the weight moves together — because a correction that
updates four of five figures is worse than one that updates none. The
disagreement would be silent.

## 2. Bales, not kilograms {#bales}

The command counts **bales**, because that is what someone counts off a
truck. Kilograms follow from the line's own per-bale weight:

```
35A   10 × 80 = 800 kg      billed
      9 × 80 = 720 kg       arrived
```

A correction therefore cannot disagree with the arithmetic the original
sheet was built on. A line with no per-bale weight recorded is refused
with an explanation rather than given an invented conversion.

## 3. What moves {#cascade}

| | |
|---|---|
| Purchase line | `qty`, `total_weight_kg`, `line_total` |
| Invoice | `subtotal`, `grand_total` |
| Freight | re-split across lines — **the charge itself does not change** |
| Landed cost | recomputed per line from the new split |
| Stock | a typed `adjustment_decrease` / `adjustment_increase` movement |
| Weighted average | unwound at the original landed cost |
| Books | one compensating journal entry, never a rewritten one |
| Payable | falls out of `grand_total − amount_paid`, so it follows automatically |

Freight is what the transporter charged; a missing bale does not refund
it. Only its allocation across the invoice moves.

## 4. What it deliberately does not do {#non-goals}

- **It does not edit `qty_on_hand` directly.** The difference is a typed
  movement, so `qty_on_hand` still equals the signed sum of its
  movements and the nightly reconciliation stays meaningful. That is
  `CLAUDE.md`'s standing acceptance criterion and a test asserts it.
- **It does not restate other products' cost.** Re-splitting freight
  changes what the *invoice* says each line cost, but only the corrected
  product's stock is moved. Restating the weighted average of products
  whose quantity never changed would mean unwinding every movement
  since, and doing that silently is how a cost basis rots. The reply
  says so out loud.
- **It does not reuse `purchase_return`.** Goods that never arrived did
  not go back to the supplier. The movement is restamped as an
  adjustment so the history reads as what happened.

## 5. The two warnings {#warnings}

Both are surfaced, never swallowed:

- **Overpaid.** Paying 1,20,000 and then correcting to 1,08,000 leaves
  12,000 sitting with the supplier. Saying nothing would let someone pay
  it a second time.
- **Cost approximated.** If most of that batch has already been sold,
  the weighted average cannot be unwound exactly (`docs/03_Inventory.md`
  §4). The quantity still falls; the average is held and flagged, rather
  than emitting a number that would quietly corrupt the cost basis of
  every later sale.

## 6. Command {#command}

```
receive <invoice> <CODE> <bales actually received> [<CODE> <bales> ...]
receive 001 35A 9
receive 001 35A 9 22D 4
```

Absolute, not a delta — "9 arrived", not "1 short". Sending it twice
says "nothing to change" instead of removing two bales.

### One truck is one command

A truck is unloaded once, so several lines are usually short together.
Every pair after the invoice is another line of the same bill, and all
of them apply **in one transaction** — half a correction would leave the
bill disagreeing with the stock behind it.

The reply states the invoice total and the payable once, from the state
after every line was applied. Quoting them per line would show three
different "still owed" figures for one bill, two of which were only ever
true mid-correction.

A code listed twice is refused rather than applied twice: two counts for
one line are two different claims about what arrived, and the second
silently winning is not a resolution.

### Said, not remembered {#wizard}

`receive` on its own asks which bill, then offers **the lines that are
actually on it** — code, bales billed, weight, rate — and loops until
you say that's all. This is the one command where remembering an invoice
number *and* a code was the whole difficulty, and both are things the
system already knows. Past ten lines the menu offers a typed escape;
several codes typed at once still work.

Unlike a sale, a single count is **never** spread across several codes.
"35A, 22D" answered "9" would claim nine bales of each — and unlike a
price, that writes stock movements nobody asked for. Zero *is* a real
answer: a bale that never turned up is the reason this command exists.
