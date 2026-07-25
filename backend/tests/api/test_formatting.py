"""Indian digit grouping and quantity formatting -- examples straight
from docs/08_WhatsApp.md."""

from __future__ import annotations

import datetime
from decimal import Decimal

from backend.api.formatting import fmt_date, fmt_money, fmt_qty


def test_fmt_money_indian_grouping() -> None:
    assert fmt_money(Decimal("452300")) == "₹4,52,300.00"
    assert fmt_money(Decimal("215000")) == "₹2,15,000.00"
    assert fmt_money(Decimal("1500")) == "₹1,500.00"
    assert fmt_money(Decimal("123")) == "₹123.00"
    assert fmt_money(Decimal("0")) == "₹0.00"
    assert fmt_money(Decimal("12345678.9")) == "₹1,23,45,678.90"
    assert fmt_money(Decimal("-1500")) == "-₹1,500.00"
    assert fmt_money(Decimal("153.2149")) == "₹153.21"


def test_fmt_qty() -> None:
    assert fmt_qty(Decimal("130")) == "130.0"
    assert fmt_qty(Decimal("12.5")) == "12.5"
    assert fmt_qty(Decimal("0.125")) == "0.125"
    assert fmt_qty(Decimal("-3")) == "-3.0"


def test_fmt_date() -> None:
    assert fmt_date(datetime.date(2026, 7, 24)) == "24-07-2026"
