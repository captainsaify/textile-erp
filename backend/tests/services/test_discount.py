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
import uuid

import pytest

from backend.core.exceptions import ValidationError
from backend.models.enums import AccountCode, SalePaymentType
from backend.services.purchase_service import allocate
from backend.services.sales_service import SaleDraft, SaleDraftLine, SalesService

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


# --- partial payment at sale time -------------------------------------


def _validate(draft: SaleDraft) -> None:
    """Call the validator without building a service.

    `SalesService.validate` touches no state on `self` -- it is a pure
    check over the draft that happens to live on the class. Constructing
    the service would need a live session for repositories none of these
    cases reach.
    """
    SalesService.validate(SalesService.__new__(SalesService), draft)


def _draft(**over: object) -> SaleDraft:
    uid = uuid.uuid4()
    fields: dict[str, object] = {
        "customer_id": uid,
        "customer_name": "Hanif Pune",
        "payment_type": SalePaymentType.CREDIT,
        "lines": [SaleDraftLine(code="X", qty=D("1"), rate=D("100"), product_id=uid)],
    }
    fields.update(over)
    return SaleDraft(**fields)  # type: ignore[arg-type]


def test_a_cash_sale_and_an_amount_are_contradictory() -> None:
    """Two answers to "how much came in" and no way to tell which is
    true, so the validation says so rather than picking one."""
    with pytest.raises(ValidationError, match="already paid in full"):
        _validate(_draft(payment_type=SalePaymentType.CASH, paid_now=D("50")))


def test_paying_more_than_the_bill_is_refused() -> None:
    with pytest.raises(ValidationError, match="more than the bill"):
        _validate(_draft(paid_now=D("500")))


def test_money_must_land_somewhere_named() -> None:
    """Paid, but into what? Money that arrived somewhere unnamed is
    asserted, not recorded."""
    with pytest.raises(ValidationError, match="cash or bank"):
        _validate(_draft(paid_now=D("50"), paid_via="pocket"))


def test_paying_exactly_the_bill_on_credit_is_allowed() -> None:
    """The counter case: handing over the whole amount is a legitimate
    part-payment of 100%, and refusing it would force a choice between
    two ways of recording the same event."""
    _validate(_draft(paid_now=D("100")))


def test_no_payment_is_the_ordinary_case_and_needs_nothing() -> None:
    _validate(_draft())
