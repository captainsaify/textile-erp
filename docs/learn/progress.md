# Learning log

The tutor reads this first and writes to it last. You don't have to
touch it — but you can, and it will believe you.

**Next session: 2 — What does one whole file look like, start to finish?**

---

## Sessions done

### Session 1 — What are the five folders, and why is that the order?
**Date:** 2026-08-15
**Files:** backend/api/commands/stock_commands.py (all of it, block by block),
backend/services/stock_service.py:67-82, backend/repositories/inventory_repository.py:38-42
**Covered:** api → services → repositories → database, one way only. Traced
`stock 55X` through all three. Then a full block-by-block read of
stock_commands.py — imports, f-strings, `async with` session, the four
branches (bare / low / not-found / multi-brand / single). The test for which
folder a rule belongs in: could you explain it to a partner with no computer
in the room? Then business rule → services. Is it only true because WhatsApp
is narrow? Then api.
**Found hard:** the check question, twice. The gap was **code vs id** — he had
never been told that `55X` is a label that can point at two rows, while every
row also has a permanent unique id. Once that landed (drawn as two tables,
products and inventory), he could see that `get_for_product(org_id,
product.id)` welds description and quantity to the same row, so "right qty,
wrong brand" has to be bad *data*, not bad code.
**Revisit:** code-vs-id will come back in session 4 (models) — check it stuck.

**Off-syllabus, answered in full:** "how does it know my business id?" — walked
whatsapp_dispatcher.py:203-235 and user_repository.py:16-25. Phone number →
user row → org_id, decided once at the door, unchangeable by anything typed.
Silence for strangers, logged before resolution. Demo mode = swapping org_id
on the detached user. He asked to defer the Redis lock (line 242) to
session 3 himself, which is the right instinct.

<!--
The tutor appends one block per session, newest at the bottom:

### Session N — <the question>
**Date:** YYYY-MM-DD
**Files:** backend/path/to/file.py
**Covered:** two or three lines, plain
**Found hard:** what needed a second pass, or "nothing"
**Revisit:** anything to come back to, or "—"
-->

---

## Things to come back to

- ~~**Possible bug, unverified.** The "Pick brand" menu sends back
  `stock 55D MKD`, which was looked up as a product *coded* "55D MKD".~~
  **Confirmed on the real thing the same day** ("Product '55d MKD' not
  found. Did you mean 55D, 55D?") and fixed: `details()` now takes an
  optional brand, an unknown brand is refused by name instead of falling
  through, and duplicate suggestions are de-duplicated. Predicted from
  reading the code in session 1, before it was ever seen happening.
- The Redis per-user lock at `whatsapp_dispatcher.py:242` — deferred to
  session 3.

---

## Questions I asked that turned into their own session

_(off-syllabus questions worth more than the planned lesson)_
