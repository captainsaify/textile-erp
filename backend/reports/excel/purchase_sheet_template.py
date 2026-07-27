"""The purchases export -- docs/13_Reports.md §5.

Column layout matches the partners' existing sheet convention:

    S.NO | QTY | DESCRIPTION | CODE | LABEL | KG | T.KG

with a bold totals row summing QTY, KG and T.KG. That layout is the
*textile product type's* export template, not a hard-coded format --
`COLUMNS` below is the same config-over-code shape `ocr_templates`
uses, so a second product type ships its own column list without this
module's structure changing (docs/00_ProjectVision.md §4).

The point of matching the legacy layout is that what comes out looks
like the sheet the partners already read. A golden-file test pins it so
an openpyxl upgrade or a refactor can't quietly drift it.
"""

from __future__ import annotations

import dataclasses
import decimal
from typing import Any

from openpyxl import Workbook

from backend.reports.excel.styling import QTY_FORMAT, autosize, write_header, write_row

ZERO = decimal.Decimal("0")

#: (header, source attribute). Order *is* the sheet's column order.
COLUMNS: list[tuple[str, str]] = [
    ("S.NO", "serial"),
    ("QTY", "pieces"),
    ("DESCRIPTION", "description"),
    ("CODE", "code"),
    ("LABEL", "label"),
    ("KG", "weight_per_unit"),
    ("T.KG", "total_weight"),
]

#: 1-indexed columns that carry a quantity and get summed in the totals
#: row -- QTY, KG, T.KG.
NUMERIC_COLUMNS = (2, 6, 7)
TOTALLED_COLUMNS = (2, 7)  # KG is a per-unit rate; summing it is meaningless


@dataclasses.dataclass(frozen=True)
class PurchaseSheetRow:
    serial: int
    pieces: decimal.Decimal | None
    description: str
    code: str
    label: str
    weight_per_unit: decimal.Decimal | None
    total_weight: decimal.Decimal | None


def build_purchase_sheet(rows: list[PurchaseSheetRow], *, title: str = "Purchases") -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = title[:31]  # Excel's hard limit on sheet names

    headers = [header for header, _ in COLUMNS]
    write_header(sheet, headers)

    formats = {index: QTY_FORMAT for index in NUMERIC_COLUMNS}
    for offset, row in enumerate(rows, start=2):
        write_row(
            sheet,
            offset,
            [_cell_value(row, attribute) for _, attribute in COLUMNS],
            formats=formats,
        )

    total_row = len(rows) + 2
    totals: list[Any] = [""] * len(COLUMNS)
    totals[0] = "TOTAL"
    for index in TOTALLED_COLUMNS:
        attribute = COLUMNS[index - 1][1]
        totals[index - 1] = sum((getattr(row, attribute) or ZERO for row in rows), ZERO)
    write_row(sheet, total_row, totals, formats=formats, bold=True)

    autosize(sheet, headers)
    sheet.freeze_panes = "A2"
    return workbook


def _cell_value(row: PurchaseSheetRow, attribute: str) -> Any:
    value = getattr(row, attribute)
    if isinstance(value, decimal.Decimal):
        # openpyxl writes Decimal as a string; float here is presentation
        # only -- every stored and computed figure stays Decimal.
        return float(value)
    return value
