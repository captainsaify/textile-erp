"""What a bill carries on top of the goods -- GST, packing, freight.

Shared by `purchase` and `sale` because the grammar has to be the same
in both. Someone who learns `GST 2240` while entering a bill from Iqbal
Bhai will type it again when billing a customer, and a word that works
in one place and silently does nothing in the other is worse than a word
that never worked at all.

Freight is kept apart from the rest. On a purchase it is allocated
across the lines by weight and so changes landed cost; everything else
only moves the total.
"""

from __future__ import annotations

import dataclasses
import decimal
import re
from typing import Protocol

#: `freight 500`, `GST: 2240`, `LBPK 2,100`.
_SET_CHARGE = re.compile(
    r"^(?P<label>[A-Za-z][\w.+&/-]*)\s*:?\s*(?P<amount>[\d,]+(?:\.\d+)?)$", re.IGNORECASE
)

#: Only these words are a charge. Without an allow-list, `BSQ 800` -- an
#: ordinary item line, and exactly what someone is part-way through
#: typing -- becomes a charge called BSQ and the bill quietly grows ₹800
#: of nothing.
CHARGE_LABELS = frozenset(
    {
        "freight",
        "transport",
        "cartage",
        "other",
        "charges",
        "gst",
        "tax",
        "packing",
        "packaging",
        "bpk",
        "lbpk",
        "labour",
        "labor",
        "loading",
        "unloading",
        "commission",
        "insurance",
        "discount",
    }
)

#: Words that mean the carrier's own freight field rather than an
#: itemised charge.
_FREIGHT_WORDS = frozenset({"freight", "transport", "cartage"})

#: Words that state one lump total, replacing any itemisation rather
#: than joining it -- "other 4340" after "GST 2240" must not bill the
#: tax twice.
_TOTAL_WORDS = frozenset({"other", "charges"})


class ChargeCarrier(Protocol):
    """A draft that can hold charges. Purchases and sales both do."""

    freight: decimal.Decimal
    other_charges: decimal.Decimal
    charges: dict[str, decimal.Decimal]


@dataclasses.dataclass(frozen=True)
class Charge:
    label: str
    amount: decimal.Decimal
    is_freight: bool
    replaces_itemisation: bool


def parse_charge(text: str) -> Charge | None:
    """A charge, or None when this is not one.

    The amount pattern only admits digits, commas and one decimal point,
    so `Decimal` cannot fail here and there is nothing to catch.
    """
    match = _SET_CHARGE.match(text.strip())
    if match is None:
        return None
    word = match["label"].lower()
    if word not in CHARGE_LABELS:
        return None
    return Charge(
        label=match["label"].upper(),
        amount=decimal.Decimal(match["amount"].replace(",", "")),
        is_freight=word in _FREIGHT_WORDS,
        replaces_itemisation=word in _TOTAL_WORDS,
    )


def apply_charge(draft: ChargeCarrier, charge: Charge) -> None:
    """Set it on the draft. Sending the same label twice re-states that
    charge; it never adds again, or correcting one would mean working
    out what to subtract."""
    if charge.is_freight:
        draft.freight = charge.amount
        return
    if charge.replaces_itemisation:
        draft.charges.clear()
    draft.charges[charge.label] = charge.amount
    draft.other_charges = sum(draft.charges.values(), decimal.Decimal("0"))


def describe(draft: ChargeCarrier) -> str:
    """`GST ₹2,240.00 + LBPK ₹2,100.00`, or empty when there is at most
    one -- a single charge reading "2240 (GST 2240)" is just noise."""
    from backend.api.formatting import fmt_money

    if len(draft.charges) < 2:
        return ""
    return " + ".join(f"{label} {fmt_money(amount)}" for label, amount in draft.charges.items())
