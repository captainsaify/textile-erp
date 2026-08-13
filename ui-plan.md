# Web app — UI/UX and frontend plan

> **Status: proposed. No code written.**
> Companion to [`plan.md`](plan.md), which covers architecture, API and
> scope. This one covers what it looks like and how it is built in the
> browser.
>
> It **extends** the existing dashboard rather than replacing it.
> [`docs/21_WebDashboard.md`](docs/21_WebDashboard.md) §5 already
> decided the stack, the auth model and the money rule, and those
> decisions stand.

---

## 1. What already exists

Worth being precise, because roughly a third of this plan is "keep doing
what is already there".

| | |
|---|---|
| `frontend/index.html` | 283 lines, 9 pages, one document |
| `frontend/assets/app.js` | 1,504 lines, one IIFE |
| `frontend/assets/styles.css` | 658 lines, custom properties, light **and** dark |
| `frontend/assets/charts.js` | 327 lines, hand-built inline SVG |
| `frontend/assets/money.js` | 87 lines, BigInt paise, Indian grouping |

**No build step, no npm, no framework.** That was a deliberate call and
it holds: one maintainer, two users, and a React toolchain would add a
lockfile and a supply chain to keep patched in exchange for ergonomics
at a scale this will not reach.

**The one hard constraint, already solved:** money never becomes a
JavaScript number. `money.js` parses to `BigInt` paise and formats with
lakh grouping. The invoice form's live totals use it as-is — this is why
running totals are safe to compute in the browser at all.

---

## 2. Principles

1. **Density over comfort.** This is a trading business's books, not a
   marketing page. A purchase bill has fourteen lines and the person
   entering it wants to see all of them. Generous whitespace is a cost
   here, not a feature.
2. **The keyboard is the primary input.** Entering a bill should never
   require the mouse. Every millisecond of hand-travel is paid fourteen
   times per bill.
3. **Numbers are the interface.** Right-aligned, tabular figures, Indian
   grouping, two decimals always. `₹1,96,340.00` — never `196340` and
   never `1.9634e5`.
4. **Warn while typing, not after saving.** Every "intelligent
   behaviour" the system already has — duplicate invoice, below cost,
   negative stock — moves from a message that arrives after the fact to
   a line of text under the field that caused it.
5. **The dangerous things must look dangerous.** And the safe things
   must not, or the warning stops meaning anything.
6. **One system, two surfaces.** A total on the screen and the same
   total in a WhatsApp reply are formatted by the same rules, because
   they are read by the same person an hour apart.

---

## 3. Design tokens

The existing palette stays. It is a warm neutral — `#fcfcfb` paper,
`#1d1c1a` ink — and both modes were chosen rather than one flipped, so
nothing here re-derives it.

**What exists:** `--surface` `--surface-raised` `--border` `--ink`
`--ink-muted` `--series-1` `--series-2` `--good` `--warn` `--bad`
`--radius` `--shadow`.

**What entry and repair need added:**

```css
:root {
  /* Density — the entry grid is tighter than the dashboard */
  --row-h: 34px;          /* one invoice line */
  --field-h: 30px;
  --gap: 8px;
  --gap-tight: 4px;

  /* Focus. One ring, everywhere, never removed without replacement */
  --focus: #2f6f9f;
  --focus-ring: 0 0 0 2px var(--surface), 0 0 0 4px var(--focus);

  /* Editable vs derived. The single most useful visual distinction
     on a data-entry screen: what you can type in, and what the
     machine computed. Derived cells are never inputs. */
  --field-bg: var(--surface-raised);
  --derived-bg: transparent;
  --derived-ink: var(--ink-muted);

  /* Danger. Deliberately not --bad: that is used for negative money
     and overdue amounts, which are ordinary. This is "this action
     destroys things". */
  --danger: #8c1d18;
  --danger-bg: #fdf3f2;
  --danger-border: #e6bcb8;
}
```

Dark mode gets its own steps for the three new colour tokens, chosen
against `#1a1a19` — not lightened automatically. `--danger-bg` in dark
is a desaturated maroon, not a pink tint, which reads as a stain rather
than a highlight.

**Type.** One family (`ui-sans-serif` stack, as now), one addition:
`font-variant-numeric: tabular-nums` on every cell containing a number.
Without it, columns of rupees do not line up and the eye cannot scan
them.

---

## 4. Navigation and information architecture

The existing dashboard has nine pages in one flat tab bar. Adding entry
and a repair console makes that ten to twenty, which is where flat stops
working.

```
┌──────────────────────────────────────────────────────────────┐
│  Wagdia Textile          [ + New bill ]  [ + New sale ]   ⚙  │  ← always visible
├──────────────────────────────────────────────────────────────┤
│  Overview  Sales  Purchases  Stock  Parties  Money  Reports  │  ← daily
└──────────────────────────────────────────────────────────────┘
```

Three rules:

- **Entry is a button in the header, not a tab.** It is an action, and
  it is the most frequent one. It opens a full-screen view, not a modal
  — a fourteen-line bill does not belong in a dialog.
- **Master Control is behind the gear, not in the tab bar.** It is used
  monthly, not daily. Putting it beside `Sales` invites a mis-click into
  a screen that can merge two customers.
- **The tab bar keeps its current nine-ish items.** They work.

Master Control gets its own left rail, since its tree is deep:

```
┌ Master Control ─────────────────────────────────────────────┐
│ DATA          │                                             │
│  Parties      │   (content)                                 │
│  Products     │                                             │
│  Contacts     │                                             │
│ TRANSACTIONS  │                                             │
│  Sales        │                                             │
│  …            │                                             │
│ ☢ DANGER      │                                             │
└───────────────┴─────────────────────────────────────────────┘
```

---

## 5. The invoice entry screen

The centrepiece. Everything else in this document is in service of it.

```
┌────────────────────────────────────────────────────────────────────────┐
│  ← Purchases            New purchase bill                    ⌘S Save   │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  Supplier *                        Invoice no *      Date *            │
│  ┌──────────────────────────┐     ┌──────────┐      ┌────────────┐     │
│  │ SHAHNAWAZ TEXTILE      ▾ │     │ 009      │      │ 13/08/2026 │     │
│  └──────────────────────────┘     └──────────┘      └────────────┘     │
│    outstanding ₹1,96,340            ⚠ similar to 007 (6 Aug)           │
│                                                                        │
│  ──────────────────────────────────────────────────────────────────    │
│                                                                        │
│   #  Item                            Qty        Rate       Amount      │
│  ┌──┬──────────────────────────────┬─────────┬──────────┬───────────┬─┐│
│  │ 1│ 55X · BSQ        0 on hand ▾ │     800 │   120.00 │ 96,000.00 │×││
│  │ 2│ 44D · MKD     1,520 on hand ▾│     640 │   107.00 │ 68,480.00 │×││
│  │ 3│ Type a code or name…       ▾ │         │          │           │ ││
│  └──┴──────────────────────────────┴─────────┴──────────┴───────────┴─┘│
│                                          ↵ adds a row                  │
│                                                                        │
│  ──────────────────────────────────────────────────────────────────    │
│                                                                        │
│  Charges                                     Subtotal    1,64,480.00   │
│  ┌─────────┬──────────┐  [+ add]             GST             1,200.00  │
│  │ GST     │  1,200   │                      Packing           800.00  │
│  │ Packing │    800   │                      ─────────────────────────  │
│  └─────────┴──────────┘                      TOTAL        1,66,480.00  │
│                                                                        │
│  Notes ┌──────────────────────────────────────────────────────────┐    │
│        │                                                          │    │
│        └──────────────────────────────────────────────────────────┘    │
│                                                                        │
│                                    [ Discard ]      [ Save bill ]      │
└────────────────────────────────────────────────────────────────────────┘
```

### 5.1 The item picker is the whole point

This one control is why the web form beats the chat.

```
┌ 55x                                          ▾ ┐
├────────────────────────────────────────────────┤
│  55X · MKD    ZIPPER SWEATER      160 on hand  │
│  55X · BSQ    ZIPPER SWEATER        0 on hand  │
│  55X · AR     ZIPPER SWEAT          0 on hand  │
├────────────────────────────────────────────────┤
│  + Create 55X under a new brand…               │
└────────────────────────────────────────────────┘
```

A code is unique **per brand**, not globally — three products share
`55X` on these books. Over WhatsApp that ambiguity is a follow-up
question, and when something answered it silently the result was one
delivery entered as two bills, 007 and 007B. On a screen it is a list
you look at. The stock figure is there because it is the number that
tells you which one you meant.

Search matches code *and* description, so `zipper` finds all three.

### 5.2 What the form does while you type

| trigger | shows |
|---|---|
| invoice no leaves focus | ⚠ *similar to 007 (6 Aug, same supplier)* — the existing fuzzy duplicate check |
| supplier chosen | their outstanding, so you know what you owe before adding to it |
| item chosen | qty on hand, and the last rate paid for it |
| rate below average cost *(sales only)* | ⚠ *below cost — avg ₹106.70* on that line |
| qty exceeds stock *(sales only)* | ⚠ *only 160 on hand* on that line |
| any change | subtotal, charges and total recompute |

All of these already exist server-side. None of them is new logic; they
are the same checks, moved earlier.

### 5.3 Totals are advisory until the server answers

Live totals are computed in the browser with `money.js` — `BigInt`
paise, no float. But the browser is **not** the authority. On save the
server recomputes from the same services WhatsApp uses, and the response
carries its own totals, which is what gets displayed afterwards.

If the two ever disagree the UI says so loudly rather than papering over
it, because a disagreement means one of them has a bug and silence would
hide which.

### 5.4 Saving

- `⌘S` / `Ctrl+S` saves. So does the button.
- The save is **idempotent** — a key is minted when the form opens, so a
  double-submit on a slow connection returns the first result instead of
  entering the bill twice.
- Success shows the created bill with a link to its sheet — the same
  document the partners receive.
- Failure keeps every field exactly as typed. A validation error that
  clears a fourteen-line form is worse than no validation.

---

## 6. Keyboard model

| key | does |
|---|---|
| `Tab` / `Shift+Tab` | next / previous field, in visual order |
| `Enter` in the last row | add a row and focus its item field |
| `Enter` in the item picker | select the highlighted option |
| `↑ ↓` | move through picker options |
| `Esc` | close the picker; again, discard the form (with confirm) |
| `⌘S` / `Ctrl+S` | save |
| `⌘K` / `Ctrl+K` | jump to search from anywhere |
| `Alt+↑ ↓` | move between rows in the same column |

The rule behind it: **a whole bill can be entered without the mouse
leaving the desk.** That is testable, and §13 makes it a test.

---

## 7. Component inventory

Small, and mostly plain HTML. Nothing here needs a framework.

| component | notes |
|---|---|
| `combo` | text input + filtered list + keyboard nav. Used for supplier, customer, item. The only genuinely fiddly one |
| `grid` | the line-item table. Rows are `<tr>`, cells are inputs or derived text |
| `money-input` | right-aligned, tabular, formats on blur, stores the raw string |
| `stat` | label + number, for the totals block |
| `banner` | already exists — inline warnings and errors |
| `sheet` | already exists — the document overlay |
| `confirm-typed` | for the repair console: an action, its preview, and a field you must type an exact value into |
| `preview` | the dry-run result: what changes, and the three balance ticks |

---

## 8. The repair console's visual language

Master Control must not look like the dashboard, or a mis-click reads
the same as a page change.

- **A different surface.** Master Control pages sit on `--surface` with
  a visible left rail; dashboard pages are cards on `--surface-raised`.
- **Every destructive action is a Preview → Confirm pair**, never a
  single button. The preview is a real dry-run (see `plan.md` §6).
- **Danger zone is visually quarantined:** `--danger-bg` panel,
  `--danger-border`, the ☢ mark, and it is the only place in the app
  with a red-filled button. Everywhere else red is text on the ordinary
  surface.
- **Confirmation is typing the thing.** Never a checkbox and never
  `y/n`. `ADMIN.md` earned that rule; the web keeps it.

```
┌ ☢ Hard delete ─────────────────────────────────────────────┐
│                                                            │
│  Purchase 1051 · SHAHNAWAZ TEXTILE · ₹1,96,340             │
│                                                            │
│  PREVIEW                                                   │
│   • 14 lines, 14 movements, 3 journal entries              │
│   • stock reversed on 9 products                           │
│   ✓ stock balances   ✓ ledgers balance   ✓ no negative     │
│                                                            │
│   Reversible with Restore purged.                          │
│                                                            │
│  Type the invoice number to confirm                        │
│  ┌────────────────────┐                                    │
│  │                    │            [ Cancel ]  [ Purge ]   │
│  └────────────────────┘                                    │
└────────────────────────────────────────────────────────────┘
```

---

## 9. Responsive stance — honest version

**Entry is designed for a laptop.** A fourteen-line invoice grid with
autocomplete on a 390px phone is a worse experience than WhatsApp, and
pretending otherwise would produce a screen nobody uses on either
device. The layout does not break on a phone — it stacks and remains
usable for a two-line bill — but it is not the target.

**Everything else is designed for the phone too**, because that is
where the dashboard is read. Tables scroll horizontally inside their
own container; the page body never does.

If phone entry turns out to matter, the answer is a separate cut-down
"quick sale" form, not a responsive version of this grid. That is a
later decision, deliberately not made now.

---

## 10. Accessibility

Not for compliance — for the 3 a.m. case.

- Every input has a real `<label>`; placeholders are never the only label.
- The combo is a proper `role="combobox"` with `aria-expanded`,
  `aria-activedescendant` and arrow-key navigation, so it works without
  sight of the mouse cursor.
- Focus is always visible. `--focus-ring` is never removed without a
  replacement.
- Warnings are `aria-live="polite"`; errors that block saving are
  `assertive`.
- Colour is never the only signal: below-cost is an icon plus text plus
  colour, because the person reading it may be doing so on a phone in
  sunlight.
- Contrast is checked, not judged — the existing palette was validated
  and the three new tokens get the same treatment.

---

## 11. Frontend architecture

`app.js` is 1,504 lines in a single IIFE. Adding entry and a repair
console would take it past 4,000, and that is the point where one file
stops being a simplification.

**Split into ES modules. Still no build step** — browsers load
`<script type="module">` natively, which is the whole reason the
no-npm decision survives this growth.

```
frontend/
  index.html            dashboard shell
  control.html          master control shell (separate document, separate auth)
  assets/
    core/
      api.js            fetch + auth refresh, one place
      money.js          exists
      dom.js            tiny helpers, no jQuery revival
      keys.js           keyboard map
    components/
      combo.js
      grid.js
      confirm-typed.js
      preview.js
    pages/
      overview.js  sales.js  purchases.js  …   (from today's app.js)
      entry-purchase.js
      entry-sale.js
      control/*.js
    styles/
      tokens.css  base.css  components.css  pages.css
```

Two deliberate calls:

- **`control.html` is a separate document**, not a route in the
  dashboard. Different auth, different session, different lifetime — one
  page that is sometimes privileged is one bug away from being always
  privileged.
- **No client-side router.** Multi-page navigation with real URLs. The
  server already serves static files; a router would be replacing
  something that works with something to maintain.

---

## 12. Deliberately not doing

- **No framework, no npm, no build step.** Restated because the pressure
  to add one arrives exactly when the combo component gets fiddly.
- **No component library.** Eight components, all small; a dependency
  would be larger than the thing it replaces.
- **No client-side money arithmetic in floats.** Ever. `money.js` or
  nothing.
- **No optimistic UI.** A saved bill appears when the server says it
  saved. Showing it early and reconciling later is how a failed write
  looks identical to a successful one.
- **No dark-mode toggle.** It follows the system, as now. A toggle is a
  preference to store, sync and get wrong.
- **No inline editing on list pages.** Editing happens on an edit
  screen, with a save button. A grid where clicking a cell changes the
  books is how you change the books by accident.

---

## 13. Build order and how it is tested

| step | contents |
|---|---|
| **F0** | Split `app.js` into modules; no visible change. Prove the no-build-step module setup on the existing pages first |
| **F1** | Tokens, `combo`, `grid`, `money-input` — with a static harness page |
| **F2** | Purchase entry, wired to `POST /purchases` |
| **F3** | Sale entry |
| **F4** | `control.html` shell, auth, left rail |
| **F5** | `preview` + `confirm-typed`, one operation end to end |
| **F6** | The rest of Master Control, screen by screen |

**Testing**, given there is no framework and no test runner in the
frontend today:

- The **API** is already covered by pytest and stays the real safety net;
  every warning the form shows has a server-side test behind it.
- **Playwright** for three flows only, run in CI: enter a purchase
  without touching the mouse, enter a sale below cost and see the
  warning, purge a bill and confirm the typed-confirmation gate. Three
  tests that fail loudly beat forty that nobody runs.
- **A contrast and palette check** in CI for the new tokens, matching
  how the chart colours are already validated rather than judged.

---

## 14. Open questions

1. **Print.** Do you ever print a bill from the browser, or is the
   WhatsApp sheet always the artefact? Changes whether print CSS is
   worth writing.
2. **Item creation from the entry form.** The picker offers *"create
   under a new brand"*. Should it also create an entirely unknown code
   mid-bill, or must products exist first? The CLI deliberately refuses;
   the form has a person watching, which is a different situation.
3. **Draft bills.** If the browser closes mid-entry, should the form
   come back? Local draft storage is easy and means an unsaved bill
   survives a closed laptop — but it is another place business data
   lives, which cuts against everything else here.
