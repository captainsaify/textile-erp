# 33. Drafts

A bill is half typed when the phone rings. The tab gets closed, the
laptop dies, the other partner picks it up on a different machine.

The form already kept a draft — in one browser's `localStorage`, in one
slot. Starting a sale overwrote a half-typed purchase, neither partner
could see what the other had left unfinished, and a draft that only
exists on the laptop that typed it does not survive the thing it exists
to survive.

## 1. Not a business record

A draft lives in its own `drafts` table, **not** in `purchase_headers`
with `status='draft'`.

This is the decision everything else follows from. A draft may name a
supplier who does not exist yet, a code that resolves to nothing, a
quantity half-typed, a total that does not add up — that is what makes
it a draft. In the books, every query that totals a payable, reconciles
inventory or builds a statement would have to remember to exclude it,
and the first one that forgets reports a number nobody can explain.

So the payload is opaque JSON: exactly what the form held, read back
only by the form that wrote it. **Nothing in the drafts path can move
money or stock.**

## 2. It becomes a record through the front door

There is no "record it" button on the Drafts page, and that is
deliberate. A draft is precisely the thing that has not been checked.
A one-click record would be a way to put an unchecked bill into the
books without ever looking at it.

*Open* loads the draft back into the form it came from. The Save there
is the same Save everything else goes through, with the same duplicate
detection, the same stock checks, the same audit row. On success the
draft is deleted — a recorded bill is not still part-way through, and
leaving it on the page is how it gets entered twice.

The list carries a `ready` flag (a party, at least one line, every line
with a quantity). It is advisory, and only decides how the row reads;
the create endpoint validates for itself regardless.

## 3. Autosave, and the abrupt exit

Typing schedules a save 1.5 seconds later. The form mints a key when it
opens and keeps it for that entry's life, so a hundred keystrokes are
one row rather than a hundred near-identical ones — that is what the
`PUT` and the unique `(org_id, client_key)` index are for.

The last 1.5 seconds are the part someone was in the middle of, and the
part a debounce would lose. So `pagehide` and `visibilitychange` flush
immediately with `fetch(..., {keepalive: true})`:

- `beforeunload` does not fire on mobile; `pagehide` does.
- `keepalive` lets the request outlive the page that sent it.

Autosave never blocks and never raises. A draft is a convenience, and
must never be the reason an entry cannot be typed. `localStorage` is
kept as well, as the offline fallback.

The **Save as draft** button saves nothing autosave has not already
saved. It exists for the person who wants to be told — it confirms,
it does not rescue.

## 4. Shared, and swept

Drafts are org-scoped and labelled with who saved them: two partners,
one set of books, and "Firoz started this bill" is useful. Deliberately
not per-user private — the rest of the system makes no such distinction.

Deleting is a real delete, not a soft one. The no-hard-delete rule
exists to protect business data; a draft is not business data, and
keeping every abandoned keystroke forever makes the list useless at
exactly the moment someone needs it. `stale_draft_sweep` drops drafts
untouched for 30 days, nightly at 03:15.

## 5. Where it lives

| Piece | Where |
|---|---|
| Table | `drafts` (`backend/models/drafts.py`) |
| Save, list, delete, sweep | `backend/services/draft_service.py` |
| API | `GET/PUT/DELETE /control/drafts` |
| Nightly sweep | `stale_draft_sweep` |
| Screen | Master Control → Drafts |
