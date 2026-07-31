"""Reading a date out of a WhatsApp message.

Money is entered days or weeks after it moved -- a ledger backfilled
from a paper book is the normal case, not the exception -- so every
money command accepts an explicit day and only falls back to today when
none was given. Kept here rather than in one command module because
`paid`, `received`, `expense` and `income` must all read a date the
same way; a date accepted by one and refused by another is the kind of
inconsistency people stop trusting.
"""

from __future__ import annotations

import datetime
import re

from backend.core.exceptions import ValidationError

#: Formats people actually type, most specific first.
FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y")

#: `on 28-07-2026` / `dated 28-07-2026`, anywhere in the line.
_ON = re.compile(r"\b(?:on|dated|date)\s+(?P<date>\S+)\s*", re.IGNORECASE)

_TODAY = {"today", "aaj"}
_YESTERDAY = {"yesterday", "kal"}


def parse_date(text: str, *, today: datetime.date) -> datetime.date:
    """A single date token. Raises rather than guessing -- a payment
    filed under the wrong day is silent and hard to find later."""
    value = text.strip().lower()
    if value in _TODAY:
        return today
    if value in _YESTERDAY:
        return today - datetime.timedelta(days=1)
    for fmt in FORMATS:
        try:
            return datetime.datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise ValidationError(
        f"'{text.strip()}' isn't a date I can read. Use DD-MM-YYYY, or say 'today'."
    )


def split_date(args: str) -> tuple[str, str | None]:
    """Pull an `on <date>` clause out of a command line.

    Returns the line without it, and the raw date text if one was there.
    Not parsed here: the caller knows the org's business date, and
    "today" can only be resolved against that.

    The clause is only recognised when what follows could be a date at
    all -- otherwise "paid Cash on Delivery 500 cash" would lose two
    words out of the supplier's name. What follows and *looks* like a
    date is then parsed strictly, so a typo is refused rather than
    quietly filed under today.
    """
    match = _ON.search(args)
    if match is None:
        return args, None
    candidate = match["date"].strip().lower()
    if not (candidate[:1].isdigit() or candidate in _TODAY | _YESTERDAY):
        return args, None
    stripped = (args[: match.start()] + " " + args[match.end() :]).strip()
    return re.sub(r"\s{2,}", " ", stripped), match["date"]
