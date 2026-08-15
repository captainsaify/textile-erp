# Learning your own codebase

30–45 minutes a day. Twenty-four sessions, about five weeks.

This is the **map**, not the lessons. To take one, say:

> **teach me today's session**

or type `/learn`. Either works, and both teach you *in the
conversation* — so you can stop mid-lesson and ask something, and get
an answer before carrying on.

It reads `docs/learn/progress.md`, works out where you are, teaches one
session, and writes down what you covered. **You never have to remember
where you got to.**

---

## How this is built

Every session starts from **a question you can already ask as the owner
of the business**, and then goes to the code that answers it. Not
"here is the folder structure" — that teaches nothing and you forget it
by the next day.

Three rules the tutor is held to:

- **Three files a day, maximum.** Usually one.
- **You run something at the end** and watch it happen. Reading code you
  have never seen execute is memorisation, not understanding.
- **You explain it back.** If you can't, the session did not work and it
  gets taught again differently. That is the tutor's fault, not yours.

You are not trying to become able to write this system. You are trying
to become able to **read it, judge it, and say "no, that's wrong"** —
which is the thing that actually protects you.

---

## Part 1 — The shape of it (sessions 1–4)

| # | The question | Where |
|---|---|---|
| 1 | What are the five folders, and why is that the order? | `backend/` |
| 2 | What does one whole file look like, start to finish? | `backend/repositories/user_repository.py` — 25 lines, the smallest real thing in here |
| 3 | A message arrives from WhatsApp. What happens, in order? | `backend/api/whatsapp_dispatcher.py` |
| 4 | What is a "model", and where does a bill actually live? | `backend/models/purchases.py` |

**After part 1** you can open any file and know roughly what *kind* of
file it is and what it is allowed to do.

## Part 2 — The goods (sessions 5–9)

| # | The question | Where |
|---|---|---|
| 5 | I send a photo of a purchase sheet. What reads it? | `backend/ocr/` |
| 6 | How does a photo become a saved bill? | `backend/services/purchase_service.py` |
| 7 | **What does my stock actually cost?** | `backend/services/inventory_service.py:78` — one line, the most important arithmetic in the system |
| 8 | Why are there two tables for stock, not one? | `inventory` vs `inventory_movements` |
| 9 | Why can't I just edit the stock number? | `backend/services/cost_replay_service.py` |

**After part 2** you understand weighted average cost well enough to
catch it being wrong. You have already caught it once — session 7
revisits that, with the actual numbers.

## Part 3 — The money (sessions 10–14)

| # | The question | Where |
|---|---|---|
| 10 | What is double-entry, in the terms of this business? | `backend/services/journal_service.py:43` |
| 11 | A customer pays ₹50,000 against three bills. Who decides which? | `backend/services/settlement_service.py` |
| 12 | Where does "cash in hand" come from? | `cash_ledger`, `bank_ledger` |
| 13 | How is profit worked out, and what is left out of it? | `backend/services/profit_service.py` |
| 14 | Why is money never a decimal in code? | `Decimal` vs float, and `money.js` |

**After part 3** you can follow a rupee from a customer's hand to the
profit figure on your dashboard.

## Part 4 — Keeping it honest (sessions 15–18)

| # | The question | Where |
|---|---|---|
| 15 | Nothing is ever really deleted. Why, and how? | soft delete, `deleted_at` |
| 16 | Who did what, and when? | `backend/services/audit_service.py` |
| 17 | **How does the system prove it isn't lying to me?** | `backend/services/reconciliation_service.py` |
| 18 | What stops a repair from breaking the books? | `backend/services/admin/guard.py` |

**After part 4** you understand the safety net you have been relying on
without knowing it was there.

## Part 5 — The edges (sessions 19–21)

| # | The question | Where |
|---|---|---|
| 19 | What runs at 2am while nobody is watching? | `backend/workers/` |
| 20 | What is Master Control actually doing when I click? | `backend/api/routers/control.py` |
| 21 | How do I read a test, and what does it prove? | `backend/tests/` |

## Part 6 — Running it (sessions 22–24)

| # | The question | Where |
|---|---|---|
| 22 | What are the seven containers and what does each one do? | `docker-compose.yml` |
| 23 | How does the database change shape without losing data? | `alembic/versions/` |
| 24 | **Case study: the day the restore froze the site.** | 14 Aug 2026 — you were there |

Session 24 is the whole system in one story: a lock, a queue, a
connection pool, an HTML error page, and why the fix was to delete a
button rather than to make it more careful.

---

## The words you will keep hitting

Not a glossary to memorise — the tutor explains each one when you first
meet it. Listed so you know none of them is a thing you were supposed to
already know.

**function** · a named piece of work · **class** · a thing with data and
the operations on it · **async / await** · "start this, do something
else while it finishes" · **ORM** · Python objects that are really
database rows · **transaction** · all of it happens or none of it does ·
**migration** · a recorded change to the database's shape ·
**repository** · the only place allowed to talk to the database ·
**service** · where the business rules live · **route** · the door from
the outside world.

---

## The one rule this codebase is built on

```
route  →  service  →  repository  →  database
```

A **route** takes a request and hands it on. It decides nothing.
A **service** decides everything — is this a duplicate, does the stock
allow it, what does it cost.
A **repository** reads and writes rows. It has no opinions.

Almost every "where does this go?" question is answered by that line.
When you can say why `PurchaseService` may not run SQL, and why
`PurchaseRepository` may not decide whether a bill is a duplicate, you
are most of the way to reading this system on your own.

---

## If you miss days

Nothing breaks. The tutor picks up where you left off, and if it has
been more than a week it starts with two minutes of recap before the new
material. Sessions are meant to be self-contained enough that a gap
costs you a recap, not a restart.
