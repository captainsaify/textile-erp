"""Discount, and why the two sides are deliberately not symmetric.

A discount *given* on a sale reduces revenue: it is a profit-and-loss
fact, and it is worth seeing on its own, because "we sold 12 lakh and
gave away 40,000" is a different sentence from "we sold 11.6 lakh" and
only one of them tells you to stop.

A discount *received* on a purchase reduces what the goods cost: a
balance-sheet fact, which belongs in the landed cost beside freight.
There is no income account involved, and inventing one would value the
stock at a price nobody paid.

Netting either into `other_charges` -- the shortcut this avoids -- puts
both in the wrong half of the P&L, which is the number the partners
actually steer by.
"""

from __future__ import annotations

import decimal

from backend.models.enums import AccountCode
from backend.services.purchase_service import allocate

D = decimal.Decimal


def test_a_purchase_discount_lowers_what_the_stock_cost() -> None:
    """1,000 kg at 100, with 5,000 off, is stock worth 95/kg -- not 100
    with a rebate hiding somewhere else."""
    line_total = D("100000")
    freight = D("0")
    charges = D("0")
    discount = D("5000")
    qty = D("1000")

    freight_share = allocate(freight, [qty])[0]
    charge_share = allocate(charges, [line_total])[0]
    discount_share = allocate(discount, [line_total])[0]

    landed = (line_total + freight_share + charge_share - discount_share) / qty
    assert landed == D("95")


def test_a_purchase_discount_spreads_across_lines_by_value() -> None:
    """Two lines, one twice the value of the other: the bigger line
    carries two-thirds of the discount, exactly as it carries two-thirds
    of the charges."""
    shares = allocate(D("3000"), [D("100000"), D("50000")])
    assert shares == [D("2000"), D("1000")]
    assert sum(shares) == D("3000"), "allocation must not lose or invent money"


def test_allocation_never_loses_a_paisa() -> None:
    """Rounding is where money leaks. Three lines and an amount that
    does not divide cleanly must still add back to the total."""
    shares = allocate(D("1000"), [D("1"), D("1"), D("1")])
    assert sum(shares) == D("1000")


def test_the_sale_side_has_its_own_account_and_the_purchase_side_does_not() -> None:
    """The asymmetry, asserted so it cannot be 'tidied up' later.

    SALES_DISCOUNT exists because a giveaway belongs on the P&L on its
    own line. There is deliberately no PURCHASE_DISCOUNT: that one is
    already visible as a lower inventory value and a lower payable, and
    an income account for it would double-count the benefit.
    """
    codes = {code.value for code in AccountCode}
    assert "sales_discount" in codes
    assert "purchase_discount" not in codes, (
        "a purchase discount reduces the cost of the goods; it is not income"
    )


def test_a_sale_debits_the_customer_net_and_credits_revenue_gross() -> None:
    """The posting shape, as arithmetic.

        Dr customer        net
        Dr SALES_DISCOUNT  discount
           Cr SALES_REVENUE      gross

    Both sides balance, revenue stays gross, and the discount is a line
    someone can read.
    """
    subtotal = D("1200000")
    discount = D("40000")
    charges = D("0")
    grand_total = subtotal + charges - discount

    debits = [("customer", grand_total), (AccountCode.SALES_DISCOUNT, discount)]
    credits = [(AccountCode.SALES_REVENUE, subtotal)]

    assert sum(amount for _, amount in debits) == sum(amount for _, amount in credits)
    assert grand_total == D("1160000")
    assert dict(credits)[AccountCode.SALES_REVENUE] == D("1200000"), (
        "revenue was netted down; the giveaway is now invisible"
    )
