"""Terminal output for the admin CLI.

Deliberately plain: no colour library, no table library. This runs over
SSH on a phone tether at three in the morning, and every dependency is
one more thing that can render as escape-code soup on a terminal that
did not expect it. Colour degrades to nothing when stdout is not a TTY,
so piping to a file gives readable text.
"""

from __future__ import annotations

import decimal
import os
import sys
from collections.abc import Sequence

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def bold(text: str) -> str:
    return _c("1", text)


def dim(text: str) -> str:
    return _c("2", text)


def green(text: str) -> str:
    return _c("32", text)


def red(text: str) -> str:
    return _c("31", text)


def yellow(text: str) -> str:
    return _c("33", text)


def say(text: str = "") -> None:
    print(text)


def head(text: str) -> None:
    print(f"\n{bold(text)}")


def ok(text: str) -> None:
    print(f"  {green('✓')} {text}")


def bad(text: str) -> None:
    print(f"  {red('✗')} {text}")


def warn(text: str) -> None:
    print(f"  {yellow('!')} {text}")


def item(text: str) -> None:
    print(f"    {text}")


def money(value: decimal.Decimal) -> str:
    """Indian digit grouping. `₹1,96,340.00`, not `₹196,340.00` -- the
    books are read by people who count in lakhs, and a number grouped
    the western way has to be re-read to be believed."""
    quantised = value.quantize(decimal.Decimal("0.01"))
    sign = "-" if quantised < 0 else ""
    whole, _, frac = f"{abs(quantised):.2f}".partition(".")
    if len(whole) > 3:
        head_, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head_) > 2:
            groups.insert(0, head_[-2:])
            head_ = head_[:-2]
        if head_:
            groups.insert(0, head_)
        whole = ",".join([*groups, tail])
    return f"{sign}₹{whole}.{frac}"


def qty(value: decimal.Decimal) -> str:
    """Trailing zeros dropped -- `90` reads as a count, `90.000` reads
    as a measurement, and most of these are counts."""
    normalised = value.normalize()
    text = format(normalised, "f")
    return text


def table(rows: Sequence[Sequence[str]], headers: Sequence[str] | None = None) -> None:
    """Left-aligned, two spaces between columns, no borders. Borders eat
    width, and width is what a phone terminal has least of."""
    if not rows and not headers:
        return
    all_rows = [list(headers)] if headers else []
    all_rows += [list(r) for r in rows]
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(all_rows[0]))]
    if headers:
        print("  " + "  ".join(dim(h.ljust(widths[i])) for i, h in enumerate(headers)))
    for row in rows:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
