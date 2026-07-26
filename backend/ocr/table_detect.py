"""Table detection -- docs/07_OCR.md §3-§4.

Two strategies, because real supplier sheets come both ways: ruled
grids (morphological line extraction) and plain columns of text
(whitespace-gap projection). Neither producing a consistent grid is a
reported failure, not a forced bad guess.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np

MIN_ROW_HEIGHT = 12
MIN_COLUMN_WIDTH = 18
# Inset the crop inside the cell boundary so ruled gridlines don't end up
# in the recognizer's input -- a black border reads as stray glyphs.
CELL_INSET = 3
GRID_ALIGNMENT_THRESHOLD = 0.7  # §3: columns must align across >=70% of rows


@dataclasses.dataclass(frozen=True)
class Cell:
    row: int
    column: int
    x0: int
    y0: int
    x1: int
    y1: int

    def crop(self, image: np.ndarray) -> np.ndarray:
        height, width = image.shape[:2]
        inset_y = CELL_INSET if self.y1 - self.y0 > 4 * CELL_INSET else 0
        inset_x = CELL_INSET if self.x1 - self.x0 > 4 * CELL_INSET else 0
        y0 = max(0, self.y0 + inset_y)
        y1 = min(height, self.y1 - inset_y)
        x0 = max(0, self.x0 + inset_x)
        x1 = min(width, self.x1 - inset_x)
        if y1 <= y0 or x1 <= x0:
            return image[self.y0 : self.y1, self.x0 : self.x1]
        return image[y0:y1, x0:x1]


@dataclasses.dataclass(frozen=True)
class Grid:
    cells: list[Cell]
    row_count: int
    column_count: int
    strategy: str  # "ruled" | "whitespace"
    confidence: float  # docs/07_OCR.md §7 table_grid_confidence term

    def row(self, index: int) -> list[Cell]:
        return sorted((cell for cell in self.cells if cell.row == index), key=lambda c: c.column)


class TableDetectionError(Exception):
    """No usable grid -- caller routes to manual entry (§3)."""


def _line_positions(mask: np.ndarray, axis: int, min_gap: int, ratio: float = 0.35) -> list[int]:
    """Collapse a line mask to de-duplicated boundary coordinates.

    `ratio` is deliberately well below half the strongest line: a real
    sheet photographed slightly unevenly has fainter rules at one edge,
    and missing a single boundary merges two columns, which shifts every
    field after it onto the wrong data.
    """
    projection = mask.sum(axis=axis)
    threshold = projection.max() * ratio if projection.size and projection.max() else 0
    if threshold == 0:
        return []
    hits = np.where(projection >= threshold)[0]
    if hits.size == 0:
        return []
    positions = [int(hits[0])]
    for value in hits[1:]:
        if value - positions[-1] >= min_gap:
            positions.append(int(value))
        else:
            positions[-1] = int(value)
    return positions


def detect_ruled_grid(binary: np.ndarray) -> Grid | None:
    """Morphological horizontal/vertical line extraction (§3)."""
    height, width = binary.shape[:2]
    horizontal_size = max(20, width // 20)
    vertical_size = max(20, height // 20)

    horizontal = cv2.morphologyEx(
        binary,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (horizontal_size, 1)),
    )
    vertical = cv2.morphologyEx(
        binary,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, vertical_size)),
    )

    row_edges = _line_positions(horizontal, axis=1, min_gap=MIN_ROW_HEIGHT)
    column_edges = _line_positions(vertical, axis=0, min_gap=MIN_COLUMN_WIDTH)
    if len(row_edges) < 3 or len(column_edges) < 3:
        return None

    cells = [
        Cell(
            row=r,
            column=c,
            x0=column_edges[c],
            y0=row_edges[r],
            x1=column_edges[c + 1],
            y1=row_edges[r + 1],
        )
        for r in range(len(row_edges) - 1)
        for c in range(len(column_edges) - 1)
    ]
    return Grid(
        cells=cells,
        row_count=len(row_edges) - 1,
        column_count=len(column_edges) - 1,
        strategy="ruled",
        confidence=0.95,
    )


def _text_bands(binary: np.ndarray, axis: int, min_size: int) -> list[tuple[int, int]]:
    """Contiguous runs of ink along `axis` -- text rows or columns."""
    projection = (binary > 0).sum(axis=axis)
    inked = projection > max(1, projection.max() * 0.02) if projection.max() else projection > 0
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(inked):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= min_size:
                bands.append((start, index))
            start = None
    if start is not None and len(inked) - start >= min_size:
        bands.append((start, len(inked)))
    return bands


def detect_whitespace_grid(binary: np.ndarray) -> Grid | None:
    """Column boundaries from consistent vertical whitespace gaps (§3
    fallback, for sheets printed without ruled lines)."""
    row_bands = _text_bands(binary, axis=1, min_size=MIN_ROW_HEIGHT)
    if len(row_bands) < 2:
        return None
    column_bands = _text_bands(binary, axis=0, min_size=MIN_COLUMN_WIDTH)
    if len(column_bands) < 2:
        return None

    # a column boundary is real only if most text rows respect it (§3)
    aligned = 0
    for top, bottom in row_bands:
        strip = binary[top:bottom, :]
        strip_columns = _text_bands(strip, axis=0, min_size=1)
        if not strip_columns:
            continue
        matches = sum(
            1
            for c0, c1 in column_bands
            if any(not (s1 < c0 or s0 > c1) for s0, s1 in strip_columns)
        )
        if matches / len(column_bands) >= GRID_ALIGNMENT_THRESHOLD:
            aligned += 1
    alignment = aligned / len(row_bands)
    if alignment < GRID_ALIGNMENT_THRESHOLD:
        return None

    cells = [
        Cell(row=r, column=c, x0=x0, y0=y0, x1=x1, y1=y1)
        for r, (y0, y1) in enumerate(row_bands)
        for c, (x0, x1) in enumerate(column_bands)
    ]
    return Grid(
        cells=cells,
        row_count=len(row_bands),
        column_count=len(column_bands),
        strategy="whitespace",
        confidence=round(0.6 + 0.3 * alignment, 3),
    )


def detect(binary: np.ndarray) -> Grid:
    grid = detect_ruled_grid(binary) or detect_whitespace_grid(binary)
    if grid is None:
        raise TableDetectionError("no usable table grid detected")
    return grid
