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
