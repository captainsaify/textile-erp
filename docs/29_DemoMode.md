# 29. Demo Mode — a second business to demonstrate in

> `login as test` · `login as real` · `demo` · `reset demo`

## 1. Why

Showing someone how this system works means recording purchases, sales
and payments. Doing that in the partners' own books leaves debris that
has to be found and reversed afterwards — which has already happened
twice: a ₹15,000 receivable from a cancelled test sale sat on the
dashboard for days, and a duplicate ₹1,65,000 sale to Hanif Pune is
still open at the time of writing.

A demo mode fixes the cause rather than the symptom. Nothing recorded
during a demonstration is ever in the real business, because it is not
in the real business's `org_id`.

## 2. How it works

`organizations` has existed since the first migration precisely so this
would be possible ([02_Database.md §3.1](02_Database.md), and
[18_FutureRoadmap.md](18_FutureRoadmap.md): multi-tenancy should be *a
migration, not a rewrite*). Every business table carries `org_id` and
every query already filters on it.

So demo mode is one decision, made once, in one place:

```
dispatcher: user = load(sender)
            if demo(sender): user.org_id = DEMO_ORG_ID
            → command → service → repository → SQL
```

**No repository, service or query knows demo mode exists.** They never
stopped filtering by org, so scoping them to a different org is the
entire mechanism. The isolation is the schema's, not a flag anybody has
to remember to check — which is why it cannot be forgotten in a new
code path.

The `User` row is detached by the time a command sees it (the dispatcher
closes the session that loaded it), so the override is never written
back to `users.org_id`.

### The mode itself

Stored in Redis under `wa:demo:{sender}`, keyed by **phone number, not
user id**: the point is that one person's phone writes to two sets of
books, and the user row is the same one either way. It survives a
restart and expires after 24h.

If Redis cannot be reached the check returns **true**. A real message
landing in the demo is an inconvenience; a demo message landing in the
real books is the thing this feature exists to prevent, so the failure
direction is chosen deliberately.

## 3. The demo's seed

On first use the demo org is created and seeded **from the live
business**, not from the migration's literals: units, product types,
warehouses and the org-wide OCR templates. A demo that could not read
the same sheets or speak the same units would demonstrate a different
system than the one being sold.

Copied rows get ids derived with `uuid5(DEMO_ORG_ID, source_id)`, so
seeding is idempotent under a retry and any demo row can be traced back
to what it came from. Foreign keys between seeded tables are re-pointed
at the copies — a demo product type referencing the real org's KG unit
would work right up until somebody deleted it, and would quietly make
the two businesses share a row.

Supplier-specific OCR templates are **not** copied: they name real
suppliers.

## 4. Telling them apart

Every reply while the mode is on is prefixed:

```
🧪 DEMO — test books, not your real business.
```

Prefixed, not appended: on a long reply a footer scrolls out of view,
and someone glancing at a stock figure has to be able to tell at once
whose stock it is.

## 5. Commands

| Command | Effect |
|---|---|
| `login as test` | Switch to the demo. Creates and seeds it on first use. |
| `login as real` | Switch back. The demo's contents are kept. |
| `demo` | Which books you are on, and what the demo holds. |
| `reset demo` | Empty the demo's books, keeping its seed. |

All four are **owner-only**: they change which business every following
message writes to, which is not a decision to leave with someone who
cannot see both sets of books.

`reset demo` hard-deletes rather than soft-deleting — the one place in
this system that does. There is no audit trail worth keeping for a
business that was never real. It refuses outside demo mode, and checks
`ctx.user.org_id == DEMO_ORG_ID` a second time before deleting anything.

## 6. What is *not* covered

The **web dashboard always shows the real business.** It authenticates
against `users.org_id` in the database, which demo mode deliberately
never writes to. Demonstrating the dashboard therefore shows real
figures. If a demonstration needs the dashboard on demo data too, that
is a separate change: an org claim in the JWT and a switcher in the UI.

## 7. Tests

`backend/tests/api/test_demo_mode.py`. The property under test is not
"demo mode works" but that **a demo message cannot reach the real
business**: the tests record real transactions in demo mode and assert
the real org's row counts are unchanged, and that a demo lookup cannot
see a real supplier.
