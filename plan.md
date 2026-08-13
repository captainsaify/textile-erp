# Master Control — plan

> **Status: proposed. No code written.**
> This is the plan for the admin web app. Nothing here is built.
> The CLI it builds on is `ADMIN.md` / [`docs/31_AdminCLI.md`](docs/31_AdminCLI.md).
> What it looks like and how it is built in the browser:
> [`ui-plan.md`](ui-plan.md).

---

## 1. What this is, and what it replaces

This has **two** purposes, and the second one is the reason to build it.

**Entering.** A purchase bill with fourteen lines, typed into WhatsApp
one message at a time, is slow work that a form does better. Every field
is known in advance, the arithmetic is the machine's job, and the
ambiguities that make the chat version painful — *which* brand carries
`55X`, is this below cost, have I entered this invoice already — are
things a screen can answer while you type instead of after you send.
This is the Vyapar-shaped part, and it is what gets used every day.

**Fixing.** A fourteen-line bill with one wrong character is not a
conversation either. The CLI closed that gap and works, but it is a
terminal over SSH on a phone tether. Master Control is the same
capability with a screen.

Entry is the daily work; repair is the occasional work. **The plan is
ordered accordingly** — §9 builds invoice entry before the repair
console, which is a change from the first draft of this document.

**Neither is a second system.** Entry goes through
`PurchaseService.confirm` and `SalesService.record` — the same code
WhatsApp calls, with the same duplicate detection, freight allocation
and below-cost checks. Repair goes through the same guarded operations
`erp` calls. The web app is a front door, never a parallel
implementation. §3 is about why that matters more than anything else
here.

---

## 2. Where it sits

```
                     POSTGRESQL
                  MASTER DATABASE
                         ▲
                         │
                  GUARDED SERVICE LAYER
              (backup · reconcile-or-rollback ·
               negative-stock · audit · dry-run)
                         ▲
         ┌───────────────┼───────────────┐
         │               │               │
     WHATSAPP        erp CLI        WEB / MASTER CONTROL
   messages, prices   ssh + key      browser, own login
   reminders          today          proposed here
   collections
```

The middle layer is the point. Today the guard lives *inside* the CLI
command modules (`backend/admin/harness.py`, and helpers scattered
through `backend/admin/commands/`). Master Control cannot re-implement
it, and cannot skip it.

---

## 3. The guard contract

Every mutating Master Control endpoint must go through the same sequence
the CLI does, in the same order:

| step | why it is not optional |
|---|---|
| **baseline snapshot** | what is *already* wrong, so repairing broken books is not blocked by the breakage being repaired |
| **backup** | `pg_dump` before the transaction opens — the only thing that survives a bug in the guard itself |
| **one transaction** | the work |
| **reconcile inside it** | inventory vs movements, every ledger vs the journal |
| **negative-stock check** | reconciliation passes on −800; stock below zero is its own regression |
| **commit or roll back** | a regression means the request did not happen |
| **audit row** | actor, channel, before/after |

If a web request can reach a mutation without all seven, Master Control
becomes the way to bypass every protection built this week. That is the
failure mode to design against, not a nice-to-have.

**Work required:** the operations currently live in Typer command
functions. They move to `backend/services/admin/` as plain async
functions taking `(session, actor, …)`, and both `erp` and the API
become thin callers. No behaviour changes; the CLI's tests keep passing
and become the API's tests too.

### 3.1 Three things the web introduces that the CLI does not have

These are new risks, not inherited ones, and each needs a decision:

- **Double submission.** A terminal command runs once. A form can be
  submitted twice by a slow connection and an impatient thumb. Every
  mutating endpoint takes an **idempotency key** minted when the form is
  rendered; a repeat with the same key returns the first result rather
  than doing it again. The sales path already has
  `idempotency_key` — the same idea, applied to every admin write.

- **Concurrency.** The CLI is one person at one terminal. The web is a
  browser tab that has been open for forty minutes while a partner
  entered three sales from their phone. Every edit form carries the
  record's `updated_at`; if it changed, the save is refused with *what*
  changed rather than silently overwriting. Optimistic locking, not
  last-write-wins.

- **Preview is not free here.** `--dry-run` in a terminal is one word.
  In a browser, a destructive action that shows you the exact effect
  *before* asking to confirm is the difference between a safe tool and a
  fast one. See §6.

---

## 4. Access and authentication

**Decision taken: public, behind its own credential**, separate from the
read-only dashboard login.

That is the choice to design well rather than argue with, so:

| control | requirement |
|---|---|
| who | **the owner only.** One account, not a role system — see §11.1 |
| credential | separate from the dashboard's; not reused anywhere |
| strength | ≥ 16 characters, generated not chosen, stored `argon2` as now |
| session | its own cookie, **30-minute** idle expiry, not the dashboard's 12-hour |
| rate limit | exists on `/login` already; extend with lockout after 5 failures |
| re-auth | DANGER ZONE re-prompts for the password regardless of session age |
| audit | every action records actor, IP and channel `web-control` |
| exposure | `/control` served only over TLS; no API tokens for it |

**Stated once, then dropped:** this is the weakest link in the design.
The books are reachable from any browser on earth, and the only thing in
front of them is one string. The mitigations above make guessing
impractical, and none of them help if the password is written down or
reused. If you ever want it stronger without making it less convenient,
TOTP on the DANGER ZONE alone is about an hour of work and does not
touch the rest.

The read-only dashboard is unchanged: same login, same 12-hour session,
same pages.

---

## 5. Invoice entry — the screen that gets used daily

One screen for a purchase, the same shape for a sale. Keyboard first:
Tab between fields, Enter adds a row, nothing needs a mouse.

```
┌─ New purchase ───────────────────────────────────────────────────────┐
│  Supplier [ SHAHNAWAZ TEXTILE          ▾]  Invoice [ 009 ]           │
│  Date     [ 2026-08-13 ]                                             │
│                                                                      │
│  #  Item                        Qty      Rate      Amount            │
│  1  [55X — BSQ · 0 on hand  ▾]  [  800]  [120.00]   96,000.00   [×]  │
│  2  [44D — MKD · 1520 …     ▾]  [  640]  [107.00]   68,480.00   [×]  │
│  3  [                       ▾]                                  [+]  │
│                                                                      │
│  Charges   GST [ 1,200 ]  Packing [ 800 ]  Freight [ 0 ]             │
│                                                                      │
│                                    Subtotal      1,64,480.00         │
│                                    Charges          2,000.00         │
│                                    TOTAL         1,66,480.00         │
│                                                                      │
│  ⚠ 009 looks like invoice 007 from the same supplier (6 Aug)         │
│                                    [ Cancel ]      [ Save bill ]     │
└──────────────────────────────────────────────────────────────────────┘
```

**What the screen fixes that the chat version cannot.** These are not
new features — they are existing behaviours moved from *after you send*
to *while you type*:

- **Brand ambiguity disappears.** `55X` exists under three brands on
  these books, and picking the wrong one silently is what produced 007
  and 007B. The item dropdown shows `CODE — BRAND · qty on hand`, so
  the choice is made by looking rather than by being asked afterwards.
- **Duplicate invoice detection becomes a warning as you type it**, not
  a rejection after you have entered fourteen lines.
- **Below-cost shows on the line**, in the sale form, next to the rate
  that triggered it.
- **Stock on hand is visible while choosing**, so a sale that would go
  negative is obvious before saving rather than refused after.
- **Charges are fields, not a second command.** The whole `charge`
  workflow exists because charges arrive after the bill is confirmed;
  entered on the form there is nothing to add later.

**Deliberately kept:** the sheet. Saving still produces the same
document the partners already receive, through the same generator. The
web form changes how a bill is *entered*, not what anyone downstream
sees.

**Not in scope for entry:** OCR from a photo. That flow works over
WhatsApp and the interesting part — a sheet photographed on a phone — is
where the phone already is.

---

## 6. How a destructive action behaves

This is the interaction that has to be right, because it is where the
CLI's safety becomes a screen.

```
┌─ Merge customer ─────────────────────────────────────────┐
│                                                          │
│   Shahid Bhai Dimapur   →   Zahid Bhai Dimapur           │
│                                                          │
│   PREVIEW                                                │
│   • 3 sales move          ₹1,42,300                      │
│   • 1 receipt moves       ₹40,000                        │
│   • outstanding: Zahid ₹1,02,300 (was ₹0)                │
│   • Shahid Bhai Dimapur will stop existing               │
│                                                          │
│   ✓ stock balances    ✓ ledgers balance    ✓ no negative │
│                                                          │
│   Type the surviving name to confirm:                    │
│   [ ................................ ]                   │
│                                                          │
│                              [ Cancel ]  [ Merge ]       │
└──────────────────────────────────────────────────────────┘
```

Three rules, taken straight from the CLI because they earned their place:

1. **Preview is mandatory and is a real dry-run** — it runs the actual
   operation in a transaction that is rolled back, so the numbers shown
   are computed, not estimated.
2. **Confirmation is typing the thing, never `y` or `[✓] I understand`.**
   A checkbox is clicked before it is read.
3. **A refusal names what is blocking**, and offers the command that
   would clear it.

---

## 7. Screens

Mapping the requested tree to what exists. **"CLI"** means the logic is
built and tested and needs an endpoint plus a screen. **"New"** means the
operation does not exist anywhere yet.

### DATA

| | state | notes |
|---|---|---|
| Parties → Merge | **CLI** | `erp merge supplier/customer` |
| Parties → **De-merge** | **New** | merge is one-way today: the losing party is soft-deleted and its transactions re-pointed, with nothing recording *which* ones moved. Splitting them back apart needs the merge to write that list first — a small change to the merge, made before it is used again, not after |
| Parties → Transfer | **New** | move *selected* transactions to another party without merging — the "three sales were his, the rest weren't" case |
| Parties → Recalculate | **New** | recompute outstanding from source rows. Cheap, because **every balance here is derived** — there are no opening balances to preserve (§11.2) |
| Parties → Delete | **CLI** | soft delete exists; blocked when transactions reference it |
| Products → Merge | **Partial** | `erp merge brand` exists; merging two *products* under one brand is new |
| Products → Transfer | **CLI** | this is `fix --code/--brand`, per line |
| Products → Delete | **New** | blocked while stock or movements exist |
| Contacts → Re-link | **New** | move a WhatsApp number to a different user — the "Firoz has two numbers" case |

### TRANSACTIONS

| | state | notes |
|---|---|---|
| Sales | **CLI** | `fix sale`, `add sale`, `charge`, `purge` |
| Purchases | **CLI** | `fix purchase` incl. `--qty`, `--remove`, `--with-sales` |
| Payments | **New** | edit/reverse exists as a service (`PaymentReversalService`); no admin surface |
| Expenses | **New** | no edit path at all today |
| Stock Movements | **Read + New** | listing exists; editing a movement directly is deliberately *not* offered — see §7 |

### FINANCIAL

| | state | notes |
|---|---|---|
| Ledger Corrections | **New** | a typed correcting entry, never an edit of a posted one |
| ~~Opening Balances~~ | **dropped** | every balance is derived from transactions (§11.2). A screen to set one would be a way to create a number nothing can verify |
| Recalculate Balances | **New** | derived balances from the journal |
| Rebuild Ledger | **New** | the ledger equivalent of `stock recost` — replay from the journal |

### INVENTORY

| | state | notes |
|---|---|---|
| Stock Adjustment | **CLI** | `erp stock adjust` |
| ~~Stock Transfer~~ | **dropped** | between warehouses, and there is one warehouse and no second one coming (§11.4). Not built, not shipped disabled — a greyed-out button is a promise |
| Rebuild Inventory | **CLI** | `erp stock recost --all` |

### WHATSAPP

| | state | notes |
|---|---|---|
| Re-link Contacts | **New** | same as Contacts → Re-link |
| Message Queue | **New** | pending/failed outbound: **retry, and cancel while still queued** (§11.3) |
| Webhook Logs | **New** | inbound with signature result — the 401 trap that cost hours has no UI today |

### SYSTEM

| | state | notes |
|---|---|---|
| Users & Permissions | **Minimal** | one owner account and its password. No role editor: a permission system with one user is a way to lock yourself out (§10.1) |
| Audit Log | **API exists** | `/audit` is live and the dashboard shows it; needs filters by entity |
| Database Health | **New** | connection, sizes, partition coverage, migration head |
| Backups | **CLI** | list / create / restore |
| Integrity Check | **CLI** | `erp check` |

### ☢️ DANGER ZONE

| | state | notes |
|---|---|---|
| Hard Delete | **CLI** | `purge` — deep delete, reversible with `restore-purged` |
| Bulk Operations | **New** | see §7 — the one I would push back on |
| Restore Database | **CLI** | `erp restore` |
| Purge Data | **CLI** | `purge` |

---

## 8. Deliberately excluded

- **No raw SQL console.** The moment it exists it is what gets used at
  1 a.m., and §3's guarantee is over.
- **No direct editing of a stock movement.** Movements are the source of
  truth that `qty_on_hand` is derived *from*. Editing one by hand is how
  the two stop agreeing. Every quantity change goes through an operation
  that writes a typed movement.
- **No editing of posted journal entries.** Corrections are new entries.
  This is not ceremony; a ledger you can edit is not a ledger.
- **Bulk Operations, as asked, I would not build.** "Apply this to 40
  records" is where a single wrong click becomes 40 wrong rows, and the
  preview that makes single operations safe becomes 40 previews nobody
  reads. What I would build instead: **saved multi-select on a filtered
  list, executing one guarded operation per row, with a per-row result
  and a stop-on-first-failure default.** Same reach, and the failure is
  bounded at one.
- **No undo of a restore.** Restoring discards everything after the
  backup, including work unrelated to whatever went wrong. It stays the
  last resort it is today.

---

## 9. Build order

Each phase is usable on its own.

| phase | contents | why here |
|---|---|---|
| **0** | Auth (§4) and the `/control` shell; POST endpoints for purchase and sale over the *existing* services; idempotency | The smallest thing that can safely accept a write |
| **1** | **Invoice entry (§5)** — purchase, then sale. Item picker with brand and stock, charges, live totals, inline warnings | The daily work. Ships first because it is what you asked for and what gets used every day |
| **2** | Read screens the entry form needs anyway: parties, products, recent bills, stock | Mostly built already (~40 GET endpoints); assembling, not inventing |
| **3** | Extract `backend/services/admin/` from the CLI command modules; CLI becomes a thin caller; no behaviour change | Nothing *repair-shaped* can be built until the guard is callable from HTTP |
| **4** | The guarded write pipeline: optimistic locking, the Preview→Confirm component (§6), one operation end-to-end (`merge customer`) | The riskiest machinery, exercised on one operation |
| **5** | TRANSACTIONS repair — edit a saved sale or purchase, stock movements | What has been done by hand all week |
| **6** | DATA — parties, products, contacts | Merges and transfers |
| **7** | FINANCIAL + INVENTORY — ledger corrections, recalculate, rebuilds, adjustments | Depends on 4's pipeline being proven |
| **8** | WHATSAPP — queue, webhook logs, re-link · SYSTEM — integrity, health, backups | Operational visibility |
| **9** | DANGER ZONE | Last, deliberately: everything it depends on is proven by then |

**Phases 0–2 are a usable product on their own** — enter bills on a
screen instead of in a chat, and read them back. If nothing after phase
2 were ever built, the thing you actually asked for would exist.

Phase 3 is the one to resist skipping once repair work starts. It is the
least visible and everything from 4 onward rests on it.

---

## 10. What could go wrong

- **The guard gets bypassed by accident.** A new endpoint written in a
  hurry that forgets `guarded()`. *Mitigation:* one router-level
  dependency that refuses any mutating admin route not declaring it —
  fail closed, and a test that walks the route table.
- **Preview and commit disagree.** The preview runs, the books change,
  the commit does something different. *Mitigation:* the commit re-runs
  the preview inside its own transaction and refuses if the effect no
  longer matches what was shown. This is what §3.1's optimistic locking
  is for.
- **The password.** Covered in §4 and not re-argued here.
- **Scope.** This document is eleven sections and roughly forty screens.
  The CLI took a day and three real bugs that only appeared when it was
  run against live data. This is several times that, and the estimate to
  distrust is the one that says otherwise.

---

## 11. Answered

**10.1 Owner only.** One account, yours. No role editor and no second
tier of access — a permission system with a single user is surface area
that can only ever lock you out of your own books. If Shoyab ever needs
a view, it is the existing read-only dashboard, which already has its
own login.

**10.2 Every balance is derived.** No party carries a stored opening
balance, so *Opening Balances* is dropped from §7 rather than built. A
screen for setting one would be a way to type in a number that nothing
downstream can verify — the opposite of the property that makes
`erp check` meaningful. *Recalculate* stays, and is cheap for the same
reason: it only ever recomputes from rows that already exist.

**10.3 Message queue: retry, and cancel while queued — my call.**
Retry alone is half the feature; the case that actually happens is
noticing a wrong figure in a notice that has not gone out yet, and the
only thing worse than not being able to stop it is being able to "stop"
one that already left. So: cancel is offered **only** while a message is
still queued, disappears the moment it is handed to Meta, and the row
stays visible afterwards marked cancelled. What is never offered is
deleting the record — a message that was cancelled is a thing that
happened.

**10.4 One warehouse, no second one coming.** *Stock Transfer* is
dropped from §7 entirely rather than shipped disabled. A greyed-out
button is a promise, and this one would not be kept. The `warehouse_id`
columns stay where they are — they cost nothing and mean a second
warehouse is a migration rather than a rewrite.

**11.6 Both halves are in scope — confirmed after being offered the
cut.** Phases 3–9 were offered for removal on the grounds that they are
not "a second input method": merge, purge and rebuild are operations
WhatsApp never had, they already exist in `erp`, and dropping them would
have cost convenience rather than capability. Keeping them is a
deliberate choice, made knowing that. The repair console is roughly
two-thirds of the remaining work and none of the daily value, so if
delivery ever has to be cut short, **it is the half to cut** — phases
0–2 stand alone and phases 3–9 have `erp` as a working fallback that
does not go away.

**11.5 And the point of the whole thing.** The request was not primarily
a repair console; it was *"a proper sale/purchase input invoice like
Vyapar"*. That is §5, and it moved to the front of §9. The first
version of this plan had it as phase 3 of 7, behind machinery that
serves the occasional job rather than the daily one.
