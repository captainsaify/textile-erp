"""Header resolution, cell extraction and confidence scoring --
docs/07_OCR.md §5, §7. Column meaning comes from the template's
header_aliases (config, never hard-coded column names) so a new product
type is a new template row, not a code change."""

from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from rapidfuzz import fuzz

from backend.ocr.engines import CellText, DualEngine
from backend.ocr.table_detect import Cell, Grid

# §5 puts this at 0.7; 0.65 in practice, because denoising turns a real
# sheet's "QTY" into "Qry" (ratio 66.7) and dropping the quantity column
# is far worse than the false match risk across a vocabulary this small
# and distinctive.
HEADER_MATCH_THRESHOLD = 65
HEADER_SCAN_ROWS = 2  # §5: first 1-2 rows are header candidates

AUTO_ACCEPT_CONFIDENCE = 0.90  # §7
REVIEW_CONFIDENCE = 0.60
MANUAL_FIELD_RATIO_THRESHOLD = 0.4  # §12: too many unreadable cells

NUMERIC_FIELDS = {"qty", "weight_kg", "total_weight_kg", "rate"}
IGNORED = "ignore"


@dataclasses.dataclass(frozen=True)
class ColumnMapping:
    field: str
    header_aliases: list[str]


@dataclasses.dataclass(frozen=True)
class ResolvedColumn:
    index: int
    field: str
    header_text: str
    match_score: float


@dataclasses.dataclass(frozen=True)
class ExtractedField:
    field: str
    text: str
    confidence: float
    engine: str

    @property
    def needs_manual(self) -> bool:
        return self.confidence < REVIEW_CONFIDENCE

    @property
    def needs_review(self) -> bool:
        return REVIEW_CONFIDENCE <= self.confidence < AUTO_ACCEPT_CONFIDENCE


@dataclasses.dataclass(frozen=True)
class ExtractedRow:
    row_index: int
    fields: dict[str, ExtractedField]

    @property
    def confidence(self) -> float:
        """A line is only as good as its worst cell (§7)."""
        if not self.fields:
            return 0.0
        return min(field.confidence for field in self.fields.values())

    @property
    def is_empty(self) -> bool:
        return all(not field.text.strip() for field in self.fields.values())


@dataclasses.dataclass(frozen=True)
class ExtractionResult:
    columns: list[ResolvedColumn]
    rows: list[ExtractedRow]
    unmapped_headers: list[str]
    grid_confidence: float
    engines: list[str]

    @property
    def manual_field_ratio(self) -> float:
        total = sum(len(row.fields) for row in self.rows)
        if not total:
            return 1.0
        unreadable = sum(
            1 for row in self.rows for field in row.fields.values() if field.needs_manual
        )
        return unreadable / total

    @property
    def hard_to_read(self) -> bool:
        return self.manual_field_ratio > MANUAL_FIELD_RATIO_THRESHOLD


def score_cell(
    engine_confidence: float, grid_confidence: float, match_confidence: float | None
) -> float:
    """Composite confidence -- §7 weights."""
    if match_confidence is None:
        return round(0.7 * engine_confidence + 0.3 * grid_confidence, 3)
    return round(0.5 * engine_confidence + 0.2 * grid_confidence + 0.3 * match_confidence, 3)


def resolve_columns(
    header_texts: list[str], mappings: list[ColumnMapping]
) -> tuple[list[ResolvedColumn], list[str]]:
    """Fuzzy-match each detected header against the template's aliases."""
    resolved: list[ResolvedColumn] = []
    unmapped: list[str] = []
    for index, raw in enumerate(header_texts):
        header = raw.strip().lower()
        if not header:
            continue
        best_field: str | None = None
        best_score = 0.0
        for mapping in mappings:
            for alias in mapping.header_aliases:
                score = float(fuzz.ratio(header, alias.lower()))
                if score > best_score:
                    best_score, best_field = score, mapping.field
        if best_field is not None and best_score >= HEADER_MATCH_THRESHOLD:
            resolved.append(
                ResolvedColumn(
                    index=index,
                    field=best_field,
                    header_text=raw.strip(),
                    match_score=round(best_score / 100, 3),
                )
            )
        else:
            # never silently dropped -- surfaced to the user once (§5)
            unmapped.append(raw.strip())

    # Two columns can plausibly match one field ("Item" and "Description"
    # both look like `description`). Keep only the best-scoring claim --
    # otherwise the loser silently overwrote the winner downstream, and a
    # real sheet's Description was replaced by an adjacent Fold column.
    best_by_field: dict[str, ResolvedColumn] = {}
    for column in resolved:
        if column.field == IGNORED:
            continue
        incumbent = best_by_field.get(column.field)
        if incumbent is None or column.match_score > incumbent.match_score:
            best_by_field[column.field] = column
    kept = {id(column) for column in best_by_field.values()}
    deduped: list[ResolvedColumn] = []
    for column in resolved:
        if column.field == IGNORED or id(column) in kept:
            deduped.append(column)
        else:
            unmapped.append(column.header_text)
    return deduped, unmapped


def _read_grid_texts(
    grid: Grid, image: np.ndarray, engine: DualEngine, *, max_workers: int = 4
) -> dict[tuple[int, int], CellText]:
    """Per-cell OCR, parallel across cells within one sheet (§13)."""
    cells = [cell for cell in grid.cells if cell.x1 > cell.x0 and cell.y1 > cell.y0]

    def read(cell: Cell) -> tuple[tuple[int, int], CellText]:
        crop = cell.crop(image)
        if crop.size == 0:
            return (cell.row, cell.column), CellText("", 0.0, "none")
        return (cell.row, cell.column), engine.read(crop)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return dict(pool.map(read, cells))


def extract(
    grid: Grid,
    image: np.ndarray,
    mappings: list[ColumnMapping],
    engine: DualEngine,
) -> ExtractionResult:
    texts = _read_grid_texts(grid, image, engine)

    header_row_index = 0
    columns: list[ResolvedColumn] = []
    unmapped: list[str] = []
    for candidate in range(min(HEADER_SCAN_ROWS, grid.row_count)):
        header_texts = [
            texts.get((candidate, column), CellText("", 0.0, "none")).text
            for column in range(grid.column_count)
        ]
        candidate_columns, candidate_unmapped = resolve_columns(header_texts, mappings)
        meaningful = [c for c in candidate_columns if c.field != IGNORED]
        if len(meaningful) >= 2:
            header_row_index, columns, unmapped = candidate, candidate_columns, candidate_unmapped
            break

    rows: list[ExtractedRow] = []
    data_columns = [column for column in columns if column.field != IGNORED]
    for row_index in range(header_row_index + 1, grid.row_count):
        fields: dict[str, ExtractedField] = {}
        for column in data_columns:
            cell = texts.get((row_index, column.index), CellText("", 0.0, "none"))
            numeric = column.field in NUMERIC_FIELDS
            text = cell.text.strip()
            confidence = score_cell(
                cell.confidence, grid.confidence, None if numeric else column.match_score
            )
            if not text:
                confidence = 0.0
            fields[column.field] = ExtractedField(
                field=column.field, text=text, confidence=confidence, engine=cell.engine
            )
        row = ExtractedRow(row_index=row_index, fields=fields)
        if not row.is_empty:
            rows.append(row)

    return ExtractionResult(
        columns=columns,
        rows=rows,
        unmapped_headers=unmapped,
        grid_confidence=grid.confidence,
        engines=engine.engines_available,
    )
