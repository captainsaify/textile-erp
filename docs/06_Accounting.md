# 06 — Accounting

## 1. Two representations, one truth

The partners think in **cash/bank/capital movements** — "cash went
down ₹5,000," "ABC paid us ₹4,400 into the bank." That is what
`cash_ledger`, `bank_ledger`, and `partner_capital`
([02_Database.md §3.15–3.16](02_Database.md)) represent, and it's what
every WhatsApp command (`cash`, `bank`, `ledger`, `profit`) reads from
directly — fast, simple, matches their mental model.

Underneath, every one of those simplified entries also generates a
**double-entry `journal`/`journal_lines` pair**
([02_Database.md §3.17](02_Database.md#317-journal-double-entry-backbone)).
This is not redundant bookkeeping for its own sake — it's what makes
the Balance Sheet (§6) *provably* balance, and what makes P&L (§5)
derivable by account rollup instead of by ad hoc per-report SQL that
could drift out of sync with the simplified ledgers over time. A
`JournalService.post()` call is made from inside every service method
that also writes to `cash_ledger`/`bank_ledger`/`partner_capital` /
`inventory_movements`, in the same transaction — never as a separate
reconciliation step run later. If a transaction cannot produce a
balanced journal entry, it does not commit at all (the `CHECK
((debit = 0) <> (credit = 0))` constraint plus an application-level
"total debits == total credits per journal_id" assertion inside
`JournalService.post()`, checked before commit).

## 2. Chart of accounts (v1)

| `account_code` | Type | Increases with (per this system's usage) |
|---|---|---|
| `cash` | Asset | Debit |
| `bank` | Asset | Debit |
| `inventory` | Asset | Debit |
| `accounts_receivable` | Asset | Debit |
| `accounts_payable` | Liability | Credit |
| `partner_capital` | Equity | Credit |
| `sales_revenue` | Revenue | Credit |
| `cogs` | Expense | Debit |
| `freight_expense` | Expense | Debit |
| `operating_expenses` | Expense | Debit |
| `other_income` | Revenue | Credit |
| `damage_loss` | Expense | Debit |

Kept intentionally small and flat — this is a trading business's
books, not a chart of accounts for a manufacturer or a multi-department
company. New accounts are added by inserting a row into a future
`chart_of_accounts` config table if/when needed (documented as a
roadmap item in [18_FutureRoadmap.md](18_FutureRoadmap.md); v1 ships
with the fixed list above as a Python enum, since it does not vary per
`product_type` the way OCR templates do, and adding an account is rare
enough not to need runtime configurability yet — unlike product types,
this is not a place we're deliberately keeping generic on day one, and
that asymmetry is intentional).

## 3. Journal entries generated per transaction type

| Transaction | Debit | Credit |
|---|---|---|
| Purchase confirmed (credit) | `inventory` (grand_total) | `accounts_payable` (grand_total) |
| Purchase confirmed (paid immediately) | `inventory` | `cash`/`bank` |
| Payment to supplier (`paid`) | `accounts_payable` | `cash`/`bank` |
| Sale confirmed (credit) | `accounts_receivable` (grand_total); `cogs` (sum of qty × avg_cost_at_sale_time) | `sales_revenue` (grand_total); `inventory` (sum of qty × avg_cost_at_sale_time) |
| Sale confirmed (cash/bank) | `cash`/`bank`; `cogs` | `sales_revenue`; `inventory` |
| Payment received (`received`) | `cash`/`bank` | `accounts_receivable` |
| Purchase return | `accounts_payable` or `cash`/`bank` (refund) | `inventory` |
| Sale return | `inventory` (at historical cost) | `accounts_receivable` or `cash`/`bank` (refund) |
| Expense | `operating_expenses` (or `freight_expense` if category=freight) | `cash`/`bank` |
| Income | `cash`/`bank` | `other_income` |
| Partner capital contribution | `cash`/`bank` | `partner_capital` |
| Partner capital withdrawal | `partner_capital` | `cash`/`bank` |
| Damage / write-off | `damage_loss` | `inventory` |
| Manual inventory adjustment (increase) | `inventory` | `operating_expenses` (contra, "inventory correction") — see note |
| Manual inventory adjustment (decrease) | `operating_expenses` ("inventory correction") | `inventory` |

*Note on manual adjustments*: routing an unexplained inventory
correction through an expense-like contra account (rather than
silently changing the asset value with no revenue/expense counterpart)
is deliberate — it means every stock adjustment shows up somewhere in
the P&L as a visible line ("inventory correction: −₹1,200 this
month"), which is exactly the kind of thing an owner should see
trending, not have buried in a balance-sheet-only asset revaluation.

## 4. Weighted average cost's role in accounting

Every `cogs` debit at time of sale uses
`sales_lines.avg_cost_at_sale_time` (the snapshot taken at the moment
of sale — see
[03_Inventory.md §2](03_Inventory.md#2-weighted-average-cost--the-algorithm)),
**not** a cost recomputed at report time. This is what makes historical
P&L stable: running the same month's P&L report next year produces the
identical number, because it isn't re-deriving cost from a
present-day average that has since moved.

## 5. Profit & Loss

```
Revenue           = SUM(sales_headers.grand_total) for period, status IN (confirmed, partially_returned)
                     MINUS SUM(returned line values) for period
COGS              = SUM(sales_lines.qty * sales_lines.avg_cost_at_sale_time) for period
                     MINUS COGS reversed by returns (at the same historical cost)
Gross Profit      = Revenue - COGS
Operating Expenses = SUM(expenses.amount) for period (all categories) + freight not already
                      capitalized into inventory (freight on purchases IS capitalized — see §3 — so
                      it does not double-count here; only standalone freight/transport expenses do)
Other Income       = SUM(income.amount) for period
Damage/Write-off   = SUM(|damage movement value|) for period
Net Profit         = Gross Profit - Operating Expenses + Other Income - Damage/Write-off
```

`profit` command ([08_WhatsApp.md #profit](08_WhatsApp.md#profit))
computes this for the requested period using the account rollup from
`journal_lines` (source of truth), cross-checked in tests against the
simplified-ledger computation above to guarantee they never diverge
(see [15_Testing.md](15_Testing.md#accounting-parity-tests)).

## 6. Balance Sheet (basic)

```
Assets      = cash_balance + bank_balance + inventory_value + accounts_receivable_total
Liabilities = accounts_payable_total
Equity      = SUM(partner_capital.resulting_balance) per partner, summed
Assets      = Liabilities + Equity   -- must hold exactly; verified nightly (see §9)
```

- `inventory_value = SUM(inventory.qty_on_hand * inventory.weighted_avg_cost)`
  across all products/warehouses for the org — the same number the
  `AI query` "Show inventory worth more than ₹50,000" (see
  [09_AI.md](09_AI.md)) filters against, computed identically in both
  places via one shared `InventoryValuationService.total_value()`
  method rather than duplicated SQL.
- Presented as "basic" deliberately per
  [`CLAUDE.md`](../CLAUDE.md) — no depreciation schedules, no
  fixed-asset register, no accrued-but-unbilled items. Adding those is
  a roadmap item ([18_FutureRoadmap.md](18_FutureRoadmap.md)), not a
  v1 requirement, because the business doesn't currently own
  depreciable fixed assets material to the books.

## 7. Cash Flow

```
Operating Cash Flow = cash/bank inflows from sales+receipts
                       - cash/bank outflows for purchases+payments+expenses
Financing Cash Flow  = partner capital contributions - withdrawals
Net Cash Flow         = Operating + Financing
```
Computed directly from `cash_ledger` + `bank_ledger` signed amounts
for the period (these tables are already a cash-flow statement in
row form — no separate derivation needed beyond grouping and summing).

## 8. Partner capital accounting

- Each partner has an equity balance, tracked as a running total in
  `partner_capital.resulting_balance` (same append-only-with-snapshot
  pattern as every other ledger in this system).
- **Contribution**: partner puts cash/bank into the business → credits
  `partner_capital`, debits `cash`/`bank`.
- **Withdrawal**: partner draws cash/bank out → debits `partner_capital`,
  credits `cash`/`bank`.

### Dual approval, in both directions {#dual-approval-withdrawals}

A capital movement at or above its threshold is created in a
`pending` state (`partner_capital.approved_by_partner_ids` starting
empty) and requires a **second** partner's WhatsApp confirmation before
any money moves. Two thresholds, because the two directions carry
different risk:

| Direction | Setting | Default |
|---|---|---|
| Withdrawal | `capital_withdrawal_dual_approval_threshold` | ₹25,000 |
| Contribution | `capital_contribution_dual_approval_threshold` | **₹0 — every one** |

Withdrawals were gated first: a large capital draw is exactly the kind
of transaction where a compromised phone or an impulsive decision has
outsized, hard-to-reverse consequences for the other partner's equity.

**Money in is gated too, and by default always.** Capital is not just
cash — it is ownership and profit share. A partner who records a
contribution nobody else saw has decided how the profit splits, and
`partners.profit_share_percent` is not what protects against that. The
partners asked for this directly. Zero is the default so nothing slips
through on size; a business that finds it heavy for small top-ups can
raise the threshold without losing the gate on the amounts that matter.

This is the one deliberate exception to "either partner's number is
fully trusted for everything" from
[00_ProjectVision.md §5](00_ProjectVision.md#5-personas).

Both directions share one path in `CapitalService._record`, so what
they check and what they write cannot drift apart, and one pair of
commands answers either — `approve <id>` / `reject <id>`. The pending
row records which direction it was, so nobody has to remember what they
were sent. A partner still cannot approve their own request, in either
direction, and that is checked server-side rather than assumed because
it arrived from a different phone.
- **Profit allocation**: at period close (manually triggered via
  `settings`/dashboard, not automatic — profit isn't "real" for
  allocation purposes until a partner decides to book it), net profit
  for the period is split by `partners.profit_share_percent` and
  posted as `profit_allocation` entries crediting each partner's
  capital account. This does not move cash — it's a book entry
  recognizing earned equity, distinct from a withdrawal.

## 9. Cash vs. bank

- Every transaction that involves money explicitly states `cash` or
  `bank` (WhatsApp command syntax requires it, or defaults per
  [05_Sales.md §2](05_Sales.md#2-sale-command-grammar-grammar) to
  `credit` which is neither until settled).
- `transfer_to_bank` / `transfer_to_cash` ledger entry types
  (§3.15 in [02_Database.md](02_Database.md)) represent moving money
  between the two — e.g., depositing cash collections at the bank —
  posted as a matched pair (cash outflow + bank inflow, or vice versa)
  in one transaction, same pattern as warehouse transfers in
  [03_Inventory.md §8](03_Inventory.md#8-multi-warehouse).
- `cash`/`bank` WhatsApp commands ([08_WhatsApp.md](08_WhatsApp.md))
  report current balance plus a short recent-entries list, each
  computed as `resulting_balance` on the latest ledger row — O(1) read,
  never a full re-sum, verified against a full re-sum nightly (§ next).

## 10. Receivables & payables aging

`ledger CODE` / `supplier NAME` / `customer NAME` commands report
outstanding balances bucketed:

```
0–30 days | 31–60 days | 61–90 days | 90+ days
```
bucketed by `sales_headers.sale_date` / `purchase_headers.invoice_date`
relative to the outstanding (unpaid) portion of each invoice. This is
computed per invoice, not just a single running total, specifically so
"who owes us money, and since when" (an AI query example in
[09_AI.md](09_AI.md)) can answer with real aging detail, not just a
lump sum.

## 11. Tax handling {#tax-handling}

- `suppliers.gst_number` / `customers.gst_number` are captured but tax
  computation/filing is out of scope for v1 (see
  [00_ProjectVision.md §7](00_ProjectVision.md#7-non-goals-explicitly-out-of-scope-for-v1)).
  Purchase/sale totals are captured as-is from the invoice
  (tax-inclusive, matching how the reference sheets record them); a
  future `tax_lines` table is a documented roadmap item, not built
  speculatively now.

## 12. Reconciliation & integrity checks (nightly)

1. **Ledger balance re-sum**: recompute `cash`/`bank`/each partner's
   capital balance from a full sum of their respective ledger tables;
   compare against the latest `resulting_balance` snapshot. Mismatch →
   alert, same non-silent-correction pattern as
   [03_Inventory.md §6](03_Inventory.md#6-mismatch-detection).
2. **Journal balance check**: for every `journal_id`,
   `SUM(debit) = SUM(credit)`. This should be structurally guaranteed
   by `JournalService.post()` always posting balanced pairs — the
   nightly check exists as a safety net against any future code path
   that bypasses the service (caught in code review per
   [17_CodingStandards.md](17_CodingStandards.md), but verified in
   production too, because "should never happen" and "provably never
   happens" are different guarantees).
3. **Balance sheet equation check**: `Assets = Liabilities + Equity`
   (§6) recomputed and compared to zero drift.

Full job definition: [11_BackgroundWorkers.md #reconciliation](11_BackgroundWorkers.md#reconciliation).

## 13. Edge cases

- **Expense paid personally by a partner, to be reimbursed from
  business funds later**: recorded as an `expenses` row with
  `paid_by_partner_id` set and `paid_via` reflecting the partner's
  personal cash/bank — this does **not** touch the business's
  `cash_ledger`/`bank_ledger` at all (the business didn't pay); it
  instead increases that partner's capital account (an implicit
  contribution — the partner effectively lent the business that
  amount) until settled. Modeled explicitly as a `partner_capital`
  `contribution` entry generated alongside the expense, not left
  implicit.
- **Sale and its return happen in different accounting periods** (sold
  in June, returned in July): the return's reversing journal entry
  posts in July, using the June sale's historical cost — June's P&L is
  never retroactively rewritten; July's P&L shows the return as a
  revenue/COGS reduction in July. This matches standard accrual
  practice and avoids ever needing to reopen a closed period's report.
- **Currency of an amount is ambiguous in a WhatsApp message** (e.g., a
  bare number with no cash/bank/credit qualifier where one is
  required): rejected with a specific prompt for the missing
  qualifier, never defaulted silently for money-moving commands
  (defaults are only used for `payment_type` on sales, per §2 in
  [05_Sales.md](05_Sales.md), which explicitly documents its own
  default — money defaults are never silent).
- **Partner capital goes negative** (a partner has withdrawn more than
  their contributed + allocated share): allowed (a partner can draw
  down to a deficit, which is itself meaningful information — they owe
  the business), but flagged in the `dashboard` output whenever any
  partner's capital balance is negative, since it's the kind of thing
  that should never be silently normal.
