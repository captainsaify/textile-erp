# 19 — Interactive Messages (buttons and list menus)

## 1. Why {#why}

Every decision in this system is currently made by typing an exact
word: `confirm`, `create all products`, `refund cash`, `approve
withdraw a1b2c3d4`. That has produced real failures in use — a
confirmation typed blind before the preview arrived, eight consecutive
rejected `paid` attempts, and a first purchase that looked like a dead
end because the command that resolved it was never named.

WhatsApp's interactive messages remove the typing from **decisions**.
They do not remove it from **data**: a supplier name, an invoice
number and a rate still have to be typed (§6).

This is an accelerator layered on the existing command grammar, never a
replacement for it. [`CLAUDE.md`](../CLAUDE.md#non-negotiable-philosophy)
rule 5 makes every mutating feature usable start-to-finish from
WhatsApp; buttons are only available on one transport (§3), so the
typed path stays the contract and must keep working unchanged.

## 2. Platform limits {#limits}

Verified against Meta's Cloud API documentation, July 2026
([reply buttons](https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-reply-buttons-messages/),
[list messages](https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-list-messages/)).
These numbers drive most of the design decisions below, so re-check
them before implementing — they have changed before.

| | Reply buttons | List menu |
|---|---|---|
| Max options | **3 buttons** | **10 rows total**, across ≤10 sections |
| Option label | 20 chars, must be unique | 24 chars (row title) |
| Option id | 256 chars | 200 chars |
| Row description | — | 72 chars |
| Body | **1024 chars** | **4096 chars** |
| Header | 60 chars (or image/video/document) | 60 chars |
| Footer | 60 chars | 60 chars |
| Menu button label | — | 20 chars |

Three of these bite this system directly:

- **3 buttons.** Any choice with a fourth option needs a list, or a
  "something else" button that falls back to typing.
- **10 rows.** A 26-product sheet cannot become a "pick a product"
  menu. Bulk actions plus typed exceptions, not per-item selection.
- **1024-char body on button messages.** The 26-line OCR preview
  exceeds it. See §5.

## 3. Transport constraint {#transport}

Interactive messages are a **Cloud API** feature. The whatsapp-web.js
bridge (`backend/api/bridge.py`, currently dormant — see
[HANDOFF.md](../HANDOFF.md)) cannot send them.

Every interactive message therefore ships with the text it replaces,
and the sender degrades to plain text when the transport can't do
better. That is not a temporary shim: the bridge exists because Meta's
Cloud API has no group support, and if groups are ever adopted the text
path becomes the only path again.

**Rule: a flow must be completable without ever tapping a button.**

## 4. Where interactive elements go {#placement}

Ordered by how much friction each removes, judged against failures that
actually happened in use.

### 4.1 Reply buttons — decisions

| Moment | Buttons | Replaces typing |
|---|---|---|
| Unknown products after OCR | `Create all 26` · `One by one` · `Cancel` | `create all products` |
| Purchase preview | `Confirm` · `Fix a line` · `Discard` | `confirm` |
| Sale return, already-paid sale ([05 §6](05_Sales.md#6-sale-returns)) | `Refund cash` · `Refund bank` · `Credit note` | `refund cash` |
| Withdrawal approval ([06 §8](06_Accounting.md#8-partner-capital-accounting)) | `Approve` · `Reject` | `approve withdraw <8-char id>` |
| Total mismatch ([04 §5](04_Purchases.md)) | `Use invoice` · `Use calculated` · `Fix a line` | `use invoice total` |
| Below-cost sale ([05 §4](05_Sales.md)) | `Sell anyway` · `Cancel` | `confirm anyway` |
| Duplicate invoice ([04 §6](04_Purchases.md#duplicate-detection)) | `Different invoice` · `Cancel` | `confirm anyway` |

Every one of these is a 2–3 way decision — inside the button limit with
nothing left over. That is not a coincidence: a decision a person can
hold in their head is usually a decision with few options.

`Create all 26` is 13 characters; the count is interpolated and stays
inside 20 up to `Create all 99999`. Above that the label falls back to
`Create all`.

### 4.2 List menus — selection from a set

| Trigger | Rows | Notes |
|---|---|---|
| `help` / main menu | Record · Reports · Manage sections | Sections group by intent |
| `summary`, `profit`, `export` period | Today · This week · This month · This year · Custom | `Custom` prompts for a typed range |
| `export` type | Purchases · Sales · Stock | |
| `stock CODE` under several brands | one row per brand | ≤10 brands; beyond that, current text listing |
| Supplier for a photographed sheet | recent suppliers | ≤10; `Someone else` row falls back to typing |
| `rate` / `receive` — which bill | recent confirmed invoices | supplier and date in the description |
| `rate` — which lines | `Every line` + the codes on that bill | bales, weight and rate in the description |
| `receive` — which line came short | the codes on that bill | `Another code` row takes several typed at once |

Row descriptions (72 chars) carry the detail that makes a row
self-explanatory — a brand row shows its quantity and average cost, so
choosing doesn't require remembering.

### 4.3 Not worth doing

- **Per-line corrections.** `line 12 qty 90` needs two free values; a
  menu can select the line but not the new number, so it saves nothing.
- **Anything with more than 10 options and no natural grouping.**
  Truncating a list silently hides options; the text path already
  handles unbounded sets.

## 5. Message composition {#composition}

The 1024-character body on button messages is smaller than several
existing replies. Rather than truncate — which would hide line items on
a purchase preview, exactly the content the user is being asked to
check — an interactive turn is **two messages**:

1. the full detail as a normal text message (no length problem), then
2. a short interactive message: one line of context plus the buttons.

The second message's body restates the question, so the buttons are
never orphaned from what they mean if the first message scrolls away.

List menus (4096-char body) usually fit in one message.

## 6. What still requires typing {#typing}

Buttons and lists select from options the server already knows. They
cannot collect free text. These stay typed:

- supplier name, invoice number, invoice date, rate (`details …`)
- quantities and per-line corrections (`line 3 qty 90`)
- expense/income categories and amounts
- product descriptions when creating one at a time

The honest framing: **no typing for decisions, still typing for data.**

WhatsApp Flows could collect the `details` fields as a single form and
would eliminate the largest remaining block of typing. It needs a
published Flow definition and its own endpoint, so it is scoped
separately (§9) rather than folded into this work.

## 7. Implementation shape {#implementation}

Four changes, each small on its own:

1. **`CommandResult` carries an optional interactive payload.**
   A frozen `Buttons` / `ListMenu` dataclass alongside `reply`, so a
   handler declares intent and knows nothing about Cloud API JSON.
2. **`WhatsAppClient.send_interactive()`**, plus the
   `SupportsSendText` protocol gaining an optional capability check.
   A transport that can't send interactive falls back to `send_text`
   with the same `reply` string.
3. **Webhook parsing.** `WebhookMessage` gains `interactive`, carrying
   `button_reply` / `list_reply` (`id`, `title`, and `description` on
   list rows).
4. **Dispatcher routing.** An inbound tap resolves to its `id`, and the
   dispatcher feeds that string into the *same* session-reply handlers
   that process typed input.

Point 4 is the design decision that keeps this cheap: **a button press
becomes the string a typed reply would have produced.** Button ids are
the existing command vocabulary — `confirm`, `create all products`,
`refund cash` — with context appended where the flow needs it
(`approve:a1b2c3d4`). No handler learns a second way to be called, no
existing test changes meaning, and the two input paths cannot drift
into different behaviour.

Ids stay well inside the 256/200-char caps: the longest is an action
plus a UUID prefix.

## 8. Failure scenarios {#failures}

| Scenario | Behaviour |
|---|---|
| Transport can't send interactive (bridge, or Cloud API error) | Falls back to the text message. The flow still completes by typing. |
| User types the command instead of tapping | Works — the typed path is unchanged and is what the button ids map to. |
| User taps a button from an older, superseded message | The id carries its context (draft/request id); if that no longer matches the current session state, the reply says what changed rather than acting on stale intent. |
| Button tapped after the session expired | Same as typing the equivalent — "there's no draft waiting", not a silent no-op. |
| More options than the limit allows | Falls back to the text listing. Never truncate a list of choices silently. |
| Interactive send succeeds but the follow-up text fails | The interactive message carries the question in its own body (§5), so the buttons are still answerable. |

## 9. Phasing {#phasing}

**Phase 1 — infrastructure + the four highest-value confirmations.**
`CommandResult` payload, `send_interactive`, webhook parsing, dispatcher
routing; then purchase confirm, create-all, refund choice, withdrawal
approval. Most of the benefit lands here.

**Phase 2 — list menus.** Main menu, period pickers, brand
disambiguation, supplier selection.

**Phase 3 — WhatsApp Flows for `details`: not being built.**

The assessment phases 1–2 were meant to inform has now been made, and
the answer is no. Flows would collect the `details` fields as one form,
but it needs a published Flow definition, its own endpoint, and Meta's
review — for a gain that
[20_ConversationalIntake.md](20_ConversationalIntake.md) delivers more
cheaply by asking for each missing field in turn, using data the system
already has (§3 of that doc: the vision engine already reports which
fields a sheet didn't carry).

Building both would mean two ways to collect the same four values, and
the Flow would be the one that can't degrade to text for the bridge
transport. **Doc 20 Phase 1 is the successor to this phase**, and it is
unblocked now that Phase 1 here is done.

Revisit Flows only if, after doc 20 ships, typing the *unguessable*
values — invoice number and rate — is still the main irritation. A form
does not remove those; it only relocates them.

## 10. Testing {#testing}

- The `FakeSender` in `backend/tests/api/conftest.py` records text only;
  it gains interactive recording so assertions can check both the
  buttons offered and the fallback text.
- Every interactive flow is tested **twice**: once by tapping (feeding
  the button id) and once by typing the equivalent, asserting identical
  end state. That is what stops the two paths diverging.
- A test asserts every button title is ≤20 chars and every option set
  is ≤3 (or ≤10 rows) — the limits are silent failures at the API
  boundary otherwise, surfacing as a rejected send at runtime.
