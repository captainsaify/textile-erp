# 31 — The admin CLI (`erp`)

> **Status: proposed. Nothing here is built yet.**
> This document is the design, put up for approval before any code is
> written. The companion file [`ADMIN.md`](../ADMIN.md) is the sheet to
> keep open while using it.

## 1. Why this exists {#why}

Over one working session the owner asked for the following, and every
one of them had to be done by hand, by an engineer, in SQL:

| What was asked | What the system offered |
|---|---|
| "purge 1051" | nothing — soft delete only |
| "hard delete both of them" (cancelled bills) | nothing |
| bill 002's label was MKD, it was actually LALA — "correct the brand, also the stock" | nothing: brand is per *product*, not per purchase line |
| "that trp of mkd is actually 003p my bad" | nothing: no way to change a code on a confirmed line |
| "asif panipat and yakub asif are same, club them" | nothing |
| sale registered under the wrong customer — **three separate times** | nothing |
| merge bills 007 and 007B into one | nothing |
| "add the correct purchase by yourself" with charges, backdated | possible over WhatsApp, but not with full control |
| recompute weighted-average cost after rate corrections | nothing |

The pattern is not that the owner wants a database console. It is that
**the repair vocabulary is narrower than the mistake vocabulary.** The
system can record a purchase in nine ways and un-record it in one:
`undo`, which reverses the entire bill so it can be typed again. That is
the right default for a phone. It is the wrong and only option when the
mistake is one character in one field of a bill with fourteen lines.

This CLI closes that gap for the one person who owns the books, on the
one machine that holds them.

## 2. The design constraint that matters most {#constraint}

**Every command goes through the service layer. None writes SQL.**

This is not a style preference. `purchase_lines`, `inventory`,
`inventory_movements`, `journal`, `cash_ledger` and the weighted-average
cost are five representations of the same facts, and they are kept
consistent by services, not by the database. A tool that issues `UPDATE
purchase_lines SET brand_id = …` produces books that look right on the
screen it was typed into and are wrong everywhere else.

I know this specifically, not theoretically. During this session my own
repair script replayed cost history while ignoring zero-quantity
movements, silently discarded **every rate correction ever made** across
28 products, and overstated stock by roughly ₹1.3 lakh. It was caught
because the owner knew what CWW cost — not because anything checked.

So: services only, and then the safety net below, because "goes through
services" was also true of the code that got it wrong.

## 3. The safety model {#safety}

Five properties, in order of how much they matter.

### 3.1 Reconcile-or-roll-back

Every mutating command runs inside **one transaction**, and before that
transaction commits, the CLI re-runs inventory and ledger reconciliation
*inside it*. If a mismatch exists that did not exist before the command,
the transaction is rolled back and nothing happened.

```
$ erp fix purchase 002 line 3 --brand LALA
  ✓ brand: MKD → LALA
  ✓ 1 movement re-pointed, weighted average recomputed for 2 products
  ✗ reconciliation: MKD 55X now −320 (was 0)
  ROLLED BACK — nothing was changed.
```

This is the property that would have caught my ₹1.3 lakh error, the
off-by-one date filter that moved 2 of 3 sales, and the negative stock
that followed it. It converts "hope the tool is right" into "the books
balance or the command did not happen".

### 3.2 A backup before every mutation

`pg_dump --format=custom` into `data/backups/cli/` before the
transaction opens, named for the command that is about to run. Cheap —
the whole database is under 500 KB compressed — and it is the only thing
that survives a bug in the safety net itself.

### 3.3 Confirmation by typing the thing, not by typing "y"

Destructive commands require the invoice number, party name or code to
be typed back:

```
$ erp purge purchase 1051
  This permanently deletes bill 1051 (SHAHNAWAZ TEXTILE, ₹1,96,340),
  14 lines, 14 movements, 3 journal entries and 1 attachment.
  Soft delete keeps it recoverable; this does not.
  Type the invoice number to confirm: _
```

`y/n` is muscle memory. Typing `1051` is not.

### 3.4 `--dry-run` on everything

Prints the full effect — including the reconciliation result — and
commits nothing. Destructive commands print this automatically before
asking for confirmation.

### 3.5 Audited as `cli:<user>`

Every mutation writes `audit_logs` exactly as the WhatsApp path does,
with the channel recorded as CLI so the two are distinguishable
afterwards. `erp history 007` reads it back.

> **What this does not protect against.** A command that is *correctly
> executed* and *wrong to have run* — purging the right invoice number
> for the wrong reason. Reconciliation proves the books are internally
> consistent, never that they match reality. That is what the backup and
> the audit trail are for.

## 4. What is new versus what already exists {#gap}

Roughly half of this is a keyboard on top of services that already work.

**Already exists — the CLI just calls it:**

| Need | Existing service |
|---|---|
| reverse a whole bill | `UndoService.undo` |
| correct a price on a confirmed bill | `RateChangeService.change` |
| fewer bales arrived than billed | `ReceiptCorrectionService.correct` |
| GST / packing on a confirmed bill | `ChargeService.add` |
| rename a product, supplier, customer, brand | `EditService.edit` |
| record a receipt or payment | `SettlementService` |
| reconcile | `ReconciliationService` |

**Genuinely new, and where the care goes:**

1. **`purge`** — deep delete. The record leaves every report, dashboard,
   ledger, total, search result and reconciliation pass: in daily use it
   is indistinguishable from having been deleted. The rows are retained
   and hidden, so `erp restore-purged 1051` brings it back.

   This is deliberately *not* the hard `DELETE` that was asked for. The
   thing wanted was the effect — "1051 must stop existing in my books" —
   and that effect is fully delivered. What is not delivered is the
   one-way door, because the only recovery from a real hard delete is
   restoring the pre-command backup, which also discards everything done
   after it. A purge that turns out to have named the wrong invoice is
   then two mistakes, not one.

   Purging still reverses the stock and the journal, recomputes the
   weighted average for every affected product, and refuses when a later
   transaction depends on the record — a payment allocated against the
   bill — naming what is blocking rather than cascading into it.

   Mechanically this is a `purged_at` column distinct from `deleted_at`:
   soft delete already means "cancelled, still in the books", and those
   two states must not share a flag.
2. **`fix … line … --brand`** — per-line brand. Today brand lives on
   the *product*, so "this bill's LALA was labelled MKD" cannot be
   expressed. This re-points the line to the correct product (creating
   it if the code exists under no such brand), moves the movement with
   it, and recosts both sides.
3. **`fix … line … --code`** — same shape, for a code read wrongly.
4. **`fix sale … --customer`** — move a sale, its receivable and any
   allocated receipts to a different party.
5. **`merge`** — for suppliers, customers, brands and purchase bills.
   Re-points every reference, then deletes the loser. The bill case also
   sums charges and re-allocates freight across the combined lines.
6. **`stock recost`** — replay movement history and recompute the
   weighted average. Must treat a **zero-quantity movement as a
   restatement** (`avg = unit_cost`), which is exactly what my script
   got wrong.
7. **`add purchase` / `add sale`** — direct entry with backdating,
   per-line brands and charges, for reconstructing a bill that was
   purged.

## 5. The command surface {#commands}

Shape: `erp <verb> <noun> [identifier] [flags]`. Verbs are few and mean
the same thing everywhere.

```
LOOK
  erp show purchase 007          erp show sale 12
  erp show stock 55X             erp show party "Asif Panipat"
  erp history 007                what happened to this bill, and who did it

FIX WHAT IS THERE
  erp fix purchase 007 --supplier "Asif Panipat" --invoice 007B --date 2026-07-01
  erp fix purchase 007 line 3 --code 55X --brand LALA --qty 90 --rate 107
  erp fix sale 12 --customer "Zahid Bhai Dimapur"
  erp fix sale 12 line 2 --code 003P --brand MKD
  erp charge purchase 007 GST 1200 --note "shared with Sohail bhai"

ADD
  erp add purchase --supplier "…" --invoice 009 --date 2026-08-01 \
      --line "55X:90:107:BSQ:zipper sweater" --charge "GST:1200"
  erp add sale --customer "…" --line "55X:10:200" --charge "packing:1100"
      (omit any flag and it asks, one question at a time)

JOIN AND REMOVE
  erp merge supplier "Yakub Asif" into "Asif Panipat"
  erp merge purchase 007B into 007
  erp purge purchase 1051
  erp undo <id>                  the gentle version — reverses, keeps the record

CATALOGUE
  erp products [query]            every product, with what has happened to it
  erp describe 55D "SHORT SLEEVED SWEATER" --label MKD
  erp delete-product 55D --label MKD    only if nothing ever happened to it

STOCK
  erp stock adjust 55X -5 --reason damaged --note "water damage"
  erp stock recost 55X | --all

SAFETY
  erp check                      reconcile inventory + ledger, both orgs
  erp backup | erp restore <file>
```

Two flags work on every mutating command: `--dry-run` and `--yes`
(skips the typed confirmation — for scripts, never for people).

## 6. How it is invoked {#invocation}

The live database is inside the `api` container; `localhost:5432` on the
host is a stale copy, and running admin commands against it would edit a
database nobody reads. So the CLI ships as a one-line host wrapper:

```bash
#!/usr/bin/env bash
exec docker compose -f /home/ubuntu/textile-erp/docker-compose.yml \
     exec -T api python -m backend.admin "$@"
```

installed at `/usr/local/bin/erp`. The result is that on the box, from
any directory:

```
ubuntu@erp:~$ erp show purchase 007
```

`--demo` targets the demo organisation instead of the real one. Without
it, every command operates on the real books, and the prompt says so.

## 7. What I am deliberately not building {#non-goals}

- **No arbitrary SQL subcommand.** The moment `erp sql "…"` exists, it
  becomes the thing that gets used at 1 a.m., and §2 stops being true.
- **No remote access.** SSH to the box. An admin API is a second front
  door to the same power, authenticated by the same eight-character
  dashboard password.
- **No true hard `DELETE`.** `purge` is a deep delete and is reversible
  (§4.1). If a row must genuinely leave the database — a legal erasure
  request, say — that is a deliberate one-off with a DBA present, not a
  verb standing ready on the command line.
- **No bulk/`--all` on destructive verbs** except `stock recost`, which
  computes rather than destroys.

## 8. Build order {#plan}

Each phase is usable on its own; nothing is merged half-done.

| Phase | Contents | Why first |
|---|---|---|
| 1 | `erp` wrapper, `show`, `history`, `check`, `backup`, the transaction + reconcile-or-rollback harness | The safety net must exist before anything that needs it. Read-only commands prove it works. |
| 2 | `fix` for headers and lines, incl. per-line brand and code | The largest share of the real incidents |
| 3 | `add purchase` / `add sale`, `charge`, `stock adjust` | Reconstruction after a purge |
| 4 | `purge`, `merge` | Most destructive, and depends on all of the above |
| 5 | `stock recost` | Needs phase 4's merge semantics to be settled |

Tests are part of each phase, not a phase of their own: the repository
requires 95% coverage on services, and this adds service-layer code.
Every phase also adds a case to the existing reconciliation test suite
proving the new operation leaves the books balanced.

## 9. The honest risk {#risk}

This hands a loaded tool to one person. The protections are real —
reconcile-or-rollback in particular is stronger than what the WhatsApp
path has — but `purge` genuinely destroys, and `merge` genuinely
rewrites history across tables.

The alternative is not "no risk". It is the current arrangement, where
the same operations happen anyway, by hand, in ad-hoc SQL written under
time pressure, with no backup taken automatically, no reconciliation
afterwards, and no audit row. **That is how the ₹1.3 lakh error
happened.** This is the safer of the two, not the more dangerous one.
