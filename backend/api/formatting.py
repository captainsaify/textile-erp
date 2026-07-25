"""WhatsApp reply formatting. Money uses Indian digit grouping
(₹4,52,300.00) matching the partners' convention and every example in
docs/08_WhatsApp.md; dates are DD-MM-YYYY per docs/08_WhatsApp.md §6."""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal


def fmt_money(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    rupees, paise = divmod(abs(quantized), 1)
    digits = str(int(rupees))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        groups.insert(0, head)
        digits = ",".join([*groups, tail])
    paise_str = f"{int(paise * 100):02d}"
    return f"{sign}₹{digits}.{paise_str}"


def fmt_qty(qty: Decimal) -> str:
    """Up to 3 decimals, trailing zeros trimmed but always at least one
    decimal shown -- '130.0', '12.5', '0.125'."""
    quantized = qty.quantize(Decimal("0.001"))
    text = f"{quantized:.3f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


def fmt_date(value: datetime.date) -> str:
    return value.strftime("%d-%m-%Y")
