"""Party statement -- docs/13_Reports.md §5.

Everything that happened with one supplier or customer, in the order it
happened:

    DATE | TIME | TYPE | REFERENCE | PURCHASED | PAID | BALANCE

The running balance is the point. A list of bills and a list of payments
side by side does not answer "what do I owe them today"; a single
chronological column does, and it answers it the same way the partners
would work it out on paper.

Balance is *carried*, never recomputed per row from a total, so the
closing figure and the last row's balance cannot disagree.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
from typing import Any

from openpyxl import Workbook

from backend.reports.excel.styling import MONEY_FORMAT, autosize, write_header, write_row

ZERO = decimal.Decimal("0")

HEADERS = ["DATE", "TIME", "TYPE", "REFERENCE", "PURCHASED", "PAID", "BALANCE"]
MONEY_COLUMNS = (5, 6, 7)


@dataclasses.dataclass(frozen=True)
class StatementEntry:
    """One event. `debit` increases what is owed (a bill), `credit`
    reduces it (a payment)."""

    at: datetime.datetime
    kind: str
    reference: str
    debit: decimal.Decimal = ZERO
    credit: decimal.Decimal = ZERO


def build_statement(
    entries: list[StatementEntry],
    *,
    party: str,
    role: str,
    period: str,
    opening: decimal.Decimal = ZERO,
) -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Statement"
    write_statement_sheet(sheet, entries, party=party, role=role, period=period, opening=opening)
    return workbook


def write_statement_sheet(
    sheet: Any,
    entries: list[StatementEntry],
    *,
    party: str,
    role: str,
    period: str,
    opening: decimal.Decimal = ZERO,
) -> decimal.Decimal:
    """Write one party's history onto an existing worksheet and return
    the closing balance.

    Split out from `build_statement` so the ledger can repeat it once per
    party in a single workbook -- one party per tab -- without a second
    implementation of what a statement looks like.

    `opening` is what was already owed when the period began. Without it
    a July statement starts from zero and closes on a number that is not
    the payable -- true of that month alone, and read by everyone as
    wrong.
    """
    owed_label = "Owed to them" if role == "supplier" else "Owed by them"
    write_row(sheet, 1, [f"{role.capitalize()}: {party}", "", "", "", "", "", ""], bold=True)
    write_row(sheet, 2, [f"Period: {period}", "", "", "", "", "", ""], bold=True)
    write_header(sheet, HEADERS, row=3)

    formats = {index: MONEY_FORMAT for index in MONEY_COLUMNS}
    balance = opening
    purchased_total = ZERO
    paid_total = ZERO
    first_row = 4

    if opening != ZERO:
        write_row(
            sheet,
            first_row,
            ["", "", "Opening balance", "brought forward", "", "", float(opening)],
            formats=formats,
            bold=True,
        )
        first_row += 1

    for offset, entry in enumerate(sorted(entries, key=lambda e: e.at), start=first_row):
        balance += entry.debit - entry.credit
        purchased_total += entry.debit
        paid_total += entry.credit
        write_row(
            sheet,
            offset,
            [
                entry.at.strftime("%d-%m-%Y"),
                entry.at.strftime("%H:%M"),
                entry.kind,
                entry.reference,
                float(entry.debit) if entry.debit else "",
                float(entry.credit) if entry.credit else "",
                float(balance),
            ],
            formats=formats,
        )

    total_row = len(entries) + first_row
    write_row(
        sheet,
        total_row,
        ["TOTAL", "", "", "", float(purchased_total), float(paid_total), float(balance)],
        formats=formats,
        bold=True,
    )
    write_row(
        sheet,
        total_row + 1,
        [owed_label, "", "", "", "", "", float(balance)],
        formats=formats,
        bold=True,
    )

    autosize(sheet, HEADERS)
    sheet.freeze_panes = "A4"
    return balance


def closing_balance(entries: list[StatementEntry]) -> decimal.Decimal:
    return sum((entry.debit - entry.credit for entry in entries), ZERO)
