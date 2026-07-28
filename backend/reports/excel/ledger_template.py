"""The party ledger export -- docs/13_Reports.md §5.

    PARTY | OUTSTANDING | OLDEST | DAYS | LAST ACTIVITY | STATUS

A statement answers "what happened with this one party". This answers
"who should I be chasing", which needs every party side by side and the
*age* of the debt, not just its size: ₹50,000 owed for ninety days is a
different problem from ₹50,000 owed since Tuesday.

Ageing bands come from the data, not from a judgement encoded here --
the sheet marks them so a person can sort and decide.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import re
from typing import Any

from openpyxl import Workbook

from backend.reports.excel.styling import MONEY_FORMAT, autosize, write_header, write_row

ZERO = decimal.Decimal("0")

HEADERS = ["PARTY", "OUTSTANDING", "OLDEST", "DAYS", "LAST ACTIVITY", "STATUS"]
MONEY_COLUMNS = (2,)

#: Where "getting old" starts. 45 days is the point the partners chase.
DUE_SOON_DAYS = 45
OVERDUE_DAYS = 90


@dataclasses.dataclass(frozen=True)
class LedgerRow:
    name: str
    outstanding: decimal.Decimal
    oldest_date: datetime.date | None
    days_outstanding: int | None
    last_activity: datetime.date | None

    def status(self) -> str:
        if self.days_outstanding is None:
            return ""
        if self.days_outstanding >= OVERDUE_DAYS:
            return "overdue"
        if self.days_outstanding >= DUE_SOON_DAYS:
            return "ageing"
        return "current"


#: Excel forbids these in a sheet name and truncates at 31 characters.
_SHEET_NAME_BANNED = re.compile(r"[\[\]:*?/\\]")
_SHEET_NAME_LIMIT = 31


def sheet_name(party: str, taken: set[str]) -> str:
    """Two parties can easily share the first 31 characters of a name,
    and Excel silently overwrites a duplicate tab -- so a collision gets
    a suffix rather than one party's history disappearing."""
    base = (_SHEET_NAME_BANNED.sub("-", party).strip() or "Party")[:_SHEET_NAME_LIMIT]
    candidate, suffix = base, 2
    while candidate.lower() in taken:
        tail = f" ({suffix})"
        candidate = base[: _SHEET_NAME_LIMIT - len(tail)] + tail
        suffix += 1
    taken.add(candidate.lower())
    return candidate


def build_ledger(
    rows: list[LedgerRow],
    *,
    heading: str,
    as_of: datetime.date,
    histories: dict[str, list[Any]] | None = None,
    role: str = "supplier",
) -> Workbook:
    """A summary tab, then one tab per party holding that party's own
    transactions.

    The summary answers "who should I be chasing". The per-party tabs
    answer "and what is that made of" -- the next question, which used to
    mean running a separate export for every party.
    """
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Summary"

    write_row(
        sheet,
        1,
        [f"{heading} ledger as of {as_of.strftime('%d-%m-%Y')}", *[""] * (len(HEADERS) - 1)],
        bold=True,
    )
    write_header(sheet, HEADERS, row=2)

    formats = {index: MONEY_FORMAT for index in MONEY_COLUMNS}
    # largest first: the ledger is read to decide who to call
    ordered = sorted(rows, key=lambda row: row.outstanding, reverse=True)
    for offset, row in enumerate(ordered, start=3):
        write_row(
            sheet,
            offset,
            [
                row.name,
                float(row.outstanding),
                row.oldest_date.strftime("%d-%m-%Y") if row.oldest_date else "",
                row.days_outstanding if row.days_outstanding is not None else "",
                row.last_activity.strftime("%d-%m-%Y") if row.last_activity else "never",
                row.status(),
            ],
            formats=formats,
        )

    total_row = len(ordered) + 3
    total: Any = float(sum((row.outstanding for row in ordered), ZERO))
    write_row(
        sheet,
        total_row,
        ["TOTAL", total, "", "", "", ""],
        formats=formats,
        bold=True,
    )

    autosize(sheet, HEADERS)
    sheet.freeze_panes = "A3"

    if histories:
        from backend.reports.excel.statement_template import write_statement_sheet

        taken: set[str] = {"summary"}
        for row in ordered:
            entries = histories.get(row.name) or []
            if not entries:
                continue
            write_statement_sheet(
                workbook.create_sheet(sheet_name(row.name, taken)),
                entries,
                party=row.name,
                role=role,
                period="all activity",
            )
    return workbook
