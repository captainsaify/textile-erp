"""Discount is a deduction, on both sides, and nothing more.

A discount is a lower price. On a sale less was charged, so revenue is
lower; on a purchase less was paid, so the goods cost less. Neither is
an event needing an account of its own -- the gross figure was never
billed and never paid, and putting it in the books would be recording
something that did not happen. Every revenue figure downstream would
then read high, and the correction would live in a second account
nobody thinks to net off.

What it must *not* become is a negative `other_charges`. That would put
a price reduction in the same bucket as GST and packing, which are
amounts genuinely charged on top -- two different things sharing one
column and disagreeing about the sign.

The amount stays visible on the header either way, which is where a
person looks for it: on the bill.
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


def test_neither_side_gets_an_account_of_its_own() -> None:
    """Asserted so it cannot be "improved" into existence later.

    A discount account would hold a number that never happened: the
    gross was not billed on a sale and not paid on a purchase. Both
    sides are already visible -- lower revenue, lower stock value -- and
    the amount itself is on the header for anyone who wants it.
    """
    codes = {code.value for code in AccountCode}
    assert "sales_discount" not in codes
    assert "purchase_discount" not in codes


def test_a_sale_posts_revenue_at_what_was_actually_charged() -> None:
    """The posting shape, as arithmetic.

        Dr customer  net
           Cr SALES_REVENUE  net

    One line each side, and revenue equals the bill. Crediting the gross
    and debiting the difference elsewhere would balance too -- and would
    put 12 lakh of revenue in a month where 11.6 lakh was charged.
    """
    subtotal = D("1200000")
    discount = D("40000")
    charges = D("0")
    grand_total = subtotal + charges - discount

    debits = [("customer", grand_total)]
    credits = [(AccountCode.SALES_REVENUE, subtotal - discount)]

    assert sum(amount for _, amount in debits) == sum(amount for _, amount in credits)
    assert grand_total == D("1160000")
    assert dict(credits)[AccountCode.SALES_REVENUE] == D("1160000"), (
        "revenue was posted at a gross figure that was never billed"
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
