"""Synthetic purchase sheets rendered with OpenCV.

Generated rather than checked in as binaries so the ground truth lives
next to the assertion, and degraded variants (rotated, dim, unruled)
come from the same source -- docs/07_OCR.md §14.
"""

from __future__ import annotations

import cv2
import numpy as np

HEADERS = ["S.No", "Qty", "Description", "Code", "KG", "T.KG"]
ROWS = [
    ["1", "100", "Trouser Poly", "TRP", "1", "100"],
    ["2", "40", "Jogging Fabric", "MJP", "1", "40"],
    ["3", "25", "Cotton Twill", "CTW", "2", "50"],
]

_COLUMN_X = [40, 180, 330, 700, 900, 1040]
_COLUMN_W = [140, 150, 370, 200, 140, 180]
_ROW_H = 70
_TOP = 40
_FONT = cv2.FONT_HERSHEY_SIMPLEX


def render_sheet(*, ruled: bool = True, rows: list[list[str]] | None = None) -> np.ndarray:
    body = rows if rows is not None else ROWS
    height = _TOP * 2 + _ROW_H * (len(body) + 1)
    width = _COLUMN_X[-1] + _COLUMN_W[-1] + 40
    image = np.full((height, width), 255, dtype=np.uint8)

    for row_index, cells in enumerate([HEADERS, *body]):
        y = _TOP + _ROW_H * row_index
        for column_index, text in enumerate(cells):
            cv2.putText(
                image,
                text,
                (_COLUMN_X[column_index] + 10, y + 48),
                _FONT,
                1.1,
                (0,),
                2,
                cv2.LINE_AA,
            )

    if ruled:
        for row_index in range(len(body) + 2):
            y = _TOP + _ROW_H * row_index
            cv2.line(image, (30, y), (width - 30, y), (0,), 2)
        edges = [x - 10 for x in _COLUMN_X] + [width - 30]
        for x in edges:
            cv2.line(image, (x, _TOP), (x, _TOP + _ROW_H * (len(body) + 1)), (0,), 2)
    return image


def encode(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return bytes(buffer)


def sheet_bytes(*, ruled: bool = True, rows: list[list[str]] | None = None) -> bytes:
    return encode(render_sheet(ruled=ruled, rows=rows))


def rotated_sheet_bytes(angle: float = 3.0) -> bytes:
    image = render_sheet()
    height, width = image.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    rotated: np.ndarray = cv2.warpAffine(
        image,
        matrix,
        (width, height),
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=[255.0],
    )
    return encode(rotated)


# The real supplier sheet's layout: S.NO | QTY | DESCRIPTION | (unnamed,
# carries FOLD) | CODE | LABEL | KG | T.KG. The unnamed column is the
# thing that broke column alignment in the field.
WAGDIA_HEADERS = ["S.NO", "QTY", "DESCRIPTION", "", "CODE", "LABEL", "KG", "T.KG"]
WAGDIA_ROWS = [
    ["1", "10", "Men Zipper Jacket", "FOLD", "35A", "TOP", "80", "800"],
    ["2", "19", "Men Zipper Jacket B", "FOLD", "22D", "TOP", "80", "1520"],
    ["3", "12", "Children Parka", "FOLD", "CPK", "TOP", "80", "960"],
    ["10", "82", "JOGGING PANT", "FOLD", "TRP", "TOP", "90", "7380"],
    ["", "322", "TOTAL", "", "", "", "KGS", "27280"],
]

_W_X = [30, 150, 300, 760, 900, 1090, 1250, 1390]
_W_W = [110, 140, 450, 130, 180, 150, 130, 190]


def render_wagdia_sheet() -> np.ndarray:
    height = _TOP * 2 + _ROW_H * (len(WAGDIA_ROWS) + 1)
    width = _W_X[-1] + _W_W[-1] + 40
    image = np.full((height, width), 255, dtype=np.uint8)
    for row_index, cells in enumerate([WAGDIA_HEADERS, *WAGDIA_ROWS]):
        y = _TOP + _ROW_H * row_index
        for column_index, text in enumerate(cells):
            if not text:
                continue
            cv2.putText(
                image, text, (_W_X[column_index] + 8, y + 48), _FONT, 0.9, (0,), 2, cv2.LINE_AA
            )
    for row_index in range(len(WAGDIA_ROWS) + 2):
        y = _TOP + _ROW_H * row_index
        cv2.line(image, (20, y), (width - 20, y), (0,), 2)
    for x in [x - 8 for x in _W_X] + [width - 20]:
        cv2.line(image, (x, _TOP), (x, _TOP + _ROW_H * (len(WAGDIA_ROWS) + 1)), (0,), 2)
    return image


def wagdia_sheet_bytes() -> bytes:
    return encode(render_wagdia_sheet())
