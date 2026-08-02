# 22 — Group Broadcast

## 1. The constraint {#constraint}

> **Meta's Cloud API cannot send a message to a WhatsApp group.**

Not a gap in this codebase — the API has no group messaging. Everything
below exists because of that one sentence.

## 2. The shape {#shape}

Two numbers, with a deliberate asymmetry:

| | Meta Cloud API | whatsapp-web.js relay |
|---|---|---|
| Handles commands | **yes** | never |
| Reads messages | yes | never |
| Holds session state | yes | none |
| Posts to the group | can't | **only job** |
| Official / supported | yes | no |

The relay is **outbound only**. It does not read a message, run a
command, or hold a draft. There is nothing in it to get out of sync and
nothing to exploit.

That isolation is the whole point. whatsapp-web.js is an unofficial
client and its number can be banned; the project accepted that risk
knowingly. What this design buys is that the *consequence* of a ban is
losing group posts — not losing the ERP. Records, replies, approvals and
media intake all keep working on Meta.

**Therefore: a relay failure never fails the thing that triggered it.** A
confirmed purchase stays confirmed even if nobody could be told about it.

## 3. Automatic activity {#automatic}

A Celery beat sweep, every minute, reading `audit_logs`.

Not a hook inside each command, for three reasons:

1. **Only committed facts.** The audit row exists because the
   transaction succeeded. Broadcasting from inside a command could
   announce a purchase that then rolled back — and a WhatsApp message
   cannot be unsent.
2. **Nothing waits on the relay.** A partner's confirmation must never
   sit behind the fragile half of the system.
3. **One list, not thirty call sites.** Every mutation already writes an
   audit row (`CLAUDE.md` rule 3), so what gets broadcast is a filter
   over that.

`BROADCAST_ACTIONS` is deliberately not "everything". `product.created`
is excluded: one photographed sheet creates 26 of them as part of a
purchase that is itself announced, and 26 messages would train people to
ignore the channel. Bursts of the same action collapse to a count.

**A missing watermark means "start from now"**, never "replay
everything" — switching this on must not dump months of history into the
group.

The watermark advances only over activity that was actually delivered,
so a relay outage delays messages rather than losing them. The task does
not retry: posting the same activity twice is worse than posting it a
minute late.

## 4. On-demand sharing {#sharing}

`summary`, `dashboard`, `profit`, `stock`, `ledger`, `supplier`,
`customer`, `search`, `cash`, `bank` end with one button: **Share to
group**.

Two decisions worth keeping:

- **The text is parked in the session and that text is what gets
  posted** — not a re-run of the query. Re-running could produce a
  different answer a minute later, and sharing something the sender
  never saw is worse than not sharing.
- **Shareability is declared on `CommandSpec`**, not set by each
  handler. It is a property of the command; eight handlers setting a
  flag is eight places for it to drift.

Commands that *change* data never offer to share themselves — §3 covers
those, and only once they are committed.

If the relay isn't configured, no button is offered. An offer that can
only fail is worse than no offer.

A failed share says **why**. Silence would let someone believe the other
partner has seen a number they haven't.

## 5. Setup {#setup}

1. A **dedicated SIM** for the relay — not a partner's personal number.
2. `cd whatsapp-bridge && npm start`, scan the QR once.
3. Add that number to the group, and read the group's chat id from the
   bridge log (it prints ids for chats it sees).
4. In `.env`:

   ```
   GROUP_CHAT_ID=120363xxxxxxxxxxxx@g.us
   GROUP_BROADCAST_ENABLED=true
   ```

Both default to off, so an unconfigured deployment simply never
broadcasts.

## 6. What this does not do {#non-goals}

- **No inbound from the group.** Commands are not accepted there. A
  group is the wrong place for a wizard, and the relay reading messages
  would reintroduce every risk this design removes.
- **No per-partner filtering.** The group is the audience; anyone in it
  sees everything sent to it.
- **No retry of a delivered message.** Once posted, it is posted.

## 7. Telling each partner directly {#partner-notices}

The group in §1–§6 is one chat that everyone reads. This is different:
**each partner gets their own message, and it carries the sheet.**

Three people run this business and any of them can record a purchase, a
sale or a payment from their own phone. The one who typed it gets the
confirmation and the bill; the other two got nothing until somebody
opened the dashboard. So every recorded transaction now reaches the
partners who did not record it, with the same sheet the person who did
received.

Not a digest at the end of the day. A sale that shouldn't have happened
is worth hearing about while it can still be undone.

### Why a second sweep, not the same one

Same mechanism as §3 — a Celery beat sweep over `audit_logs`, every
minute — for the same three reasons: only committed facts, nothing waits
on it, one list rather than a call added to thirty services by hand.

But it is a **separate sweep with its own watermark**
(`partner_notice_watermark`). One shared watermark would let whichever
ran first swallow the other's activity. It is also **not collapsed** the
way a group post is: a group channel is skimmed and twenty lines bury
each other, whereas this is one message per transaction *because* each
one carries that transaction's sheet, and a sheet with no transaction
beside it is unreadable.

### Who hears it

**Owners with a WhatsApp number, minus the actor.**

Not the `partners` table: that is the capital-accounting entity, and the
question here is which people to tell. An owner without a partner row
still runs the business; a partner row with no linked user has no phone
to reach. A retired number — Firoz's old 9977250571 — is soft-deleted,
so it stops receiving without being erased from the record.

Excluding the actor matters more than it looks. They already have the
confirmation and the sheet in their own chat, and a duplicate arriving a
minute later reads as a second transaction.

### What it announces

`NOTIFIABLE` in `backend/services/partner_notice_service.py`, **read off
the live `audit_logs` table rather than written from memory.** §3's
`BROADCAST_ACTIONS` was not: it lists `sale.confirmed`,
`expense.recorded` and `capital.contributed`, none of which this system
has ever written — the real names are `sale.created`, `expense.created`
and `capital.contribution`. It would have announced almost nothing, and
nobody noticed because group broadcasting has never been switched on.

`product.created` and `supplier.created` stay off the list for §3's
reason: one photographed sheet creates 26 products as part of a purchase
that is itself announced.

### The sheet

Built from the row at send time, not from the message — so a bill whose
rate was corrected twice arrives as it stands now, not as it stood when
the correction was typed. Corrections are on the list as much as
originals, because the whole point is that the other two hold the
current bill rather than an old copy of it.

| Action | Sheet |
|---|---|
| `purchase.confirmed`, `purchase.rate_corrected`, `purchase.returned` | that bill |
| `purchase.receipt_corrected` | the bill the corrected **line** sits on |
| `sale.created`, `sale.returned` | that sale |
| `payment.paid`, `payment.received` | the receipt, keyed by the audit id |
| everything else | none — the headline is the whole message |

The text is sent **first and on its own**. A document that fails to
build or upload still leaves the partner knowing what happened, and only
a failure to send the *text* holds the watermark back. On a transport
with no file channel (the web.js bridge) the headline goes and the sheet
is silently skipped, rather than the notice failing.

### The watermark has to be written down {#watermark-origin}

A missing watermark means *start from now*, never *replay everything*.
That intent was right and the implementation was not: `read_watermark`
**computed** `now()` on a miss, and the row was only written after a
delivery succeeded. So every sweep asked for activity created after
*this instant*, found the window `(now, now]` empty, delivered nothing,
wrote nothing — and repeated a minute later. Nothing could ever be the
first thing sent.

It ran that way for hours, once a minute, logging `notices: 0` while
purchases were being recorded seconds earlier, and `settings` held no
watermark row at all. Three separate "he never got it" reports came out
of it.

`claim_watermark` writes the origin the first time it is asked, so
later sweeps have a fixed point to measure from — while still never
replaying history, because the claimed origin is now rather than the
beginning of the log. Both sweeps use it; the group broadcast had the
same latent bug and had simply never been switched on to reveal it.

## 8. When WhatsApp won't deliver {#delivery}

Two limits sit outside this codebase and neither announces itself.

**The 24-hour window.** A business may only send a free-form message to
someone who messaged it within the last 24 hours. A partner who spends
a day not typing falls outside it and every notice aimed at them is
refused.

**The allowed list.** While the sender is a Meta *test* number it can
only reach up to five numbers registered against it. A recipient who is
not on that list is refused with `131030` — and a **message template
does not bypass this**, which was worth establishing before building
templates to work around the wrong problem.

### Failures are read, not assumed {#delivery-receipts}

Meta returns a message id for a send it has merely *accepted*. Whether
it arrived comes back minutes later as a delivery receipt, and those
were parsed into `WebhookValue.statuses` and then never read — the
field was commented "acknowledged, never processed". An undelivered
notice was therefore indistinguishable from a delivered one.

`WhatsAppDispatcher._record_delivery` now logs every failure with the
recipient, Meta's error code and its explanation. Only failures: every
message also produces sent/delivered/read receipts, and logging those
would bury the one line worth reading.

### The daily check-in {#daily-checkin}

One message a day to each owner, at `settings.daily_checkin_hour`
(default 9, in the **org's** timezone — the task fires hourly and each
org acts only on its own hour, so the time is a setting rather than a
redeploy).

Replying to it re-opens the 24-hour window, which is what makes the
rest of the day's notices deliverable. So it has to be worth replying
to: it carries the last five updates and real figures, not a bare
"please reply". A message that earns no reply leaves the window shut,
which is the whole failure it exists to prevent.

**Missing one costs delivery, never the record.** The same list is
always available on demand — see `activity` below.

## 9. The pull half: `activity` {#activity}

`activity` returns the last ten things recorded, whoever recorded them,
each with how long ago in words a person uses — "20 min ago",
"yesterday" — because the question being asked is *has this gone in
yet*, not *at what o'clock*. `activity 20` widens it, capped at 30.

It reads the **same** `NOTIFIABLE` list the fan-out sends, so the push
and the pull cannot disagree about what happened. If `activity` showed
rows the notices leave out, one of them would be lying.

This is the answer to every delivery limit above. A partner outside the
window, a number not on the allowed list, a message lost in a busy
chat, a phone left on a shelf — none of it ends with someone asking
another partner what changed.
