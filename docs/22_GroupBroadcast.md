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
