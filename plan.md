# Master Control — plan

> **Status: proposed. No code written.**
> This is the plan for the admin web app. Nothing here is built.
> The CLI it builds on is `ADMIN.md` / [`docs/31_AdminCLI.md`](docs/31_AdminCLI.md).

---

## 1. What this is, and what it replaces

WhatsApp is the right interface for the people doing the trading. It is
the wrong interface for the person *fixing* the trading — a fourteen-line
bill with one wrong character is not a conversation.

The CLI closed that gap last week and works. But it is a terminal, over
SSH, on a phone tether. Master Control is the same capability with a
screen: see the bill, click the line, change the field, watch what it
would do, save it.

**This is not a second system.** Everything Master Control does, `erp`
already does. The web app is a second front door onto the same guarded
operations — which is the single most important constraint in this
document, and §3 is about why.

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
  fast one. See §5.

---

## 4. Access and authentication

**Decision taken: public, behind its own credential**, separate from the
read-only dashboard login.

That is the choice to design well rather than argue with, so:

| control | requirement |
|---|---|
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

## 5. How a destructive action behaves

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

## 6. Screens

Mapping the requested tree to what exists. **"CLI"** means the logic is
built and tested and needs an endpoint plus a screen. **"New"** means the
operation does not exist anywhere yet.

### DATA

| | state | notes |
|---|---|---|
| Parties → Merge | **CLI** | `erp merge supplier/customer` |
| Parties → Transfer | **New** | move *selected* transactions to another party without merging — the "three sales were his, the rest weren't" case |
| Parties → Recalculate | **New** | recompute outstanding from source rows; opening balance is an input, not a derived value |
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
| Opening Balances | **New** | per party and per ledger |
| Recalculate Balances | **New** | derived balances from the journal |
| Rebuild Ledger | **New** | the ledger equivalent of `stock recost` — replay from the journal |

### INVENTORY

| | state | notes |
|---|---|---|
| Stock Adjustment | **CLI** | `erp stock adjust` |
| Stock Transfer | **New** | between warehouses. **Inert today** — one warehouse is seeded, so this ships disabled with a note rather than pretending |
| Rebuild Inventory | **CLI** | `erp stock recost --all` |

### WHATSAPP

| | state | notes |
|---|---|---|
| Re-link Contacts | **New** | same as Contacts → Re-link |
| Message Queue | **New** | pending/failed outbound, with retry |
| Webhook Logs | **New** | inbound with signature result — the 401 trap that cost hours has no UI today |

### SYSTEM

| | state | notes |
|---|---|---|
| Users & Permissions | **Partial** | `backend/cli.py create-user` exists; no UI |
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

## 7. Deliberately excluded

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

## 8. Build order

Each phase is usable on its own.

| phase | contents | why here |
|---|---|---|
| **0** | Extract `backend/services/admin/` from the CLI command modules; CLI becomes a thin caller; no behaviour change | Nothing else can be built safely until the guard is callable from HTTP |
| **1** | Auth (§4), `/control` shell, Integrity Check, Audit Log, Database Health, Backups | Read-only and near-read-only. Proves the auth and the shell before anything can write |
| **2** | The guarded write pipeline: idempotency, optimistic locking, the Preview→Confirm component (§5), one endpoint end-to-end (`merge customer`) | The riskiest machinery, exercised on one operation |
| **3** | TRANSACTIONS — sales, purchases, stock movements | The screens replacing what has been done by hand all week |
| **4** | DATA — parties, products, contacts | Merges and transfers |
| **5** | FINANCIAL + INVENTORY — ledger corrections, opening balances, rebuilds, adjustments | Depends on 2's pipeline being proven |
| **6** | WHATSAPP — queue, webhook logs, re-link | Operational visibility |
| **7** | DANGER ZONE | Last, deliberately: everything it depends on is proven by then |

Phase 0 is the one to resist skipping. It is the least visible and the
whole plan rests on it.

---

## 9. What could go wrong

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
- **Scope.** This document is nine sections and roughly forty screens.
  The CLI took a day and three real bugs that only appeared when it was
  run against live data. This is several times that, and the estimate to
  distrust is the one that says otherwise.

---

## 10. Open questions

1. **Users & Permissions** — is Master Control owner-only, or does Shoyab
   get a limited view? Roles exist (`owner`/`staff`); nothing in this
   plan uses them yet.
2. **Opening balances** — do any parties have one today, or is every
   balance derived from transactions? Changes whether §6 FINANCIAL is a
   real screen or a migration.
3. **Message Queue** — retry only, or also *cancel* a queued message?
   Cancel is easy to build and easy to regret.
4. **Second warehouse** — is one coming? If not, Stock Transfer ships
   disabled and that is fine; if it is, it changes the inventory screens.
