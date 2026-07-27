"""Money parsing for WhatsApp input.

The system formats money with Indian digit grouping everywhere
(₹40,92,000.00 -- backend/api/formatting.py), so refusing that same
shape as *input* is the system contradicting itself. People type what
they see. Commas, spaces and a leading ₹ are all accepted and stripped.
"""

from __future__ import annotations

import decimal
import re

from backend.core.exceptions import ValidationError

TWO_PLACES = decimal.Decimal("0.01")

_STRIP = re.compile(r"[,\s₹]")
#: what could plausibly be an amount: digits with optional grouping and
#: decimals. Used to *find* the amount in a free-form line, so it stays
#: deliberately loose -- validation happens in parse_amount.
AMOUNT_TOKEN = re.compile(r"^[₹]?[\d][\d,\s]*(?:\.\d+)?$")


def looks_like_amount(token: str) -> bool:
    return bool(AMOUNT_TOKEN.match(token.strip()))


def parse_amount(raw: str, *, field: str = "Amount") -> decimal.Decimal:
    """'40,00,000' / '₹4000.50' / '4000' -> Decimal. Raises with copy
    that names the offending text rather than restating the usage."""
    cleaned = _STRIP.sub("", raw.strip())
    if not cleaned:
        raise ValidationError(f"{field} is missing.")
    try:
        amount = decimal.Decimal(cleaned)
    except decimal.InvalidOperation:
        raise ValidationError(f"'{raw.strip()}' isn't a number I can read.") from None
    if amount <= 0:
        raise ValidationError(f"{field} must be greater than zero.")
    if amount != amount.quantize(TWO_PLACES):
        raise ValidationError(f"{field} can have at most 2 decimal places.")
    return amount.quantize(TWO_PLACES)


def money_str(value: decimal.Decimal) -> str:
    """Serialise money for JSON at 2dp.

    A string, not a float -- JSON numbers are IEEE doubles and this is
    the one boundary where a value that has been Decimal all the way
    through could quietly lose precision. Quantised because raw
    arithmetic leaves artefacts like '4092000.0000000', which is the
    same number but reads like a different kind of value.
    """
    return str(value.quantize(TWO_PLACES))


def qty_str(value: decimal.Decimal) -> str:
    """Quantities carry 3dp (docs/02_Database.md NUMERIC(12,3))."""
    return str(value.quantize(decimal.Decimal("0.001")))
