"""The rule that decides whether an admin command may commit.

`_regressions` is the whole safety property in one function, so it is
tested as one: given what was already wrong and what is wrong now, does
this change get to be saved?
"""

from __future__ import annotations

import decimal

from backend.admin.console import money, qty
from backend.admin.harness import _regressions


def _d(value: str) -> decimal.Decimal:
    return decimal.Decimal(value)


def test_a_clean_change_commits() -> None:
    assert _regressions({}, {}) == []


def test_a_newly_broken_subject_blocks() -> None:
    problems = _regressions({}, {"inventory:MKD 55X": _d("320")})
    assert len(problems) == 1
    assert "MKD 55X" in problems[0]


def test_repairing_a_pre_existing_break_is_allowed() -> None:
    """The books are sometimes already wrong -- that is why someone is
    at the terminal. A repair must not be blocked by the breakage it is
    repairing."""
    before = {"inventory:CWW": _d("42")}
    assert _regressions(before, {}) == []
    assert _regressions(before, {"inventory:CWW": _d("10")}) == []


def test_making_an_existing_break_worse_blocks() -> None:
    """Comparing only "was it listed" would let a command double an
    existing mismatch and call it no change."""
    problems = _regressions({"inventory:CWW": _d("10")}, {"inventory:CWW": _d("40")})
    assert len(problems) == 1
    assert "was off by 10, now off by 40" in problems[0]


def test_swapping_one_problem_for_another_blocks() -> None:
    """Same count before and after, entirely different books."""
    problems = _regressions({"inventory:A": _d("5")}, {"inventory:B": _d("5")})
    assert len(problems) == 1
    assert "inventory:B" in problems[0]


def test_an_unchanged_break_is_not_a_regression() -> None:
    same = {"ledger:cash": _d("7")}
    assert _regressions(same, dict(same)) == []


def test_money_groups_the_indian_way() -> None:
    """`1,96,340` not `196,340`. The books are read by people who count
    in lakhs; western grouping has to be re-read to be believed."""
    assert money(_d("196340")) == "₹1,96,340.00"
    assert money(_d("1100")) == "₹1,100.00"
    assert money(_d("110000")) == "₹1,10,000.00"
    assert money(_d("999")) == "₹999.00"
    assert money(_d("-1360.5")) == "-₹1,360.50"
    assert money(_d("0")) == "₹0.00"


def test_quantity_drops_trailing_zeros() -> None:
    """`90` reads as a count; `90.000` reads as a measurement."""
    assert qty(_d("90.000")) == "90"
    assert qty(_d("2.500")) == "2.5"
    assert qty(_d("0")) == "0"
