"""OCR engines -- docs/07_OCR.md §6, dual-engine strategy.

PaddleOCR is the primary recognizer and Tesseract the fallback for
low-confidence cells; on disagreement the higher-confidence result wins
rather than a fixed engine preference. Both engines load lazily and a
missing/broken engine degrades to the other rather than failing the
pipeline -- Paddle in particular is a heavy optional import.
"""

from __future__ import annotations

import dataclasses
import re
import threading
from typing import Any, Protocol

import cv2
import numpy as np

from backend.core.logging import get_logger

logger = get_logger(__name__)

FALLBACK_THRESHOLD = 0.75  # settings.ocr_engine_fallback_threshold default

# applied to numeric cells only -- never to code/description, where these
# characters can be legitimately meaningful (§6)
_DIGIT_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})
_NON_NUMERIC = re.compile(r"[^0-9.,]")

# Recognizers need breathing room and reasonably sized glyphs: a bare
# tight crop of a short cell ("TRP", "100") reads as nothing at all.
CELL_BORDER_PX = 12
MIN_CELL_HEIGHT_PX = 40


def prepare_cell(image: np.ndarray) -> np.ndarray:
    """Upscale small crops and add a white quiet zone."""
    if image.size == 0:
        return image
    height = image.shape[0]
    if height < MIN_CELL_HEIGHT_PX:
        scale = MIN_CELL_HEIGHT_PX / max(1, height)
        image = cv2.resize(
            image,
            (max(1, int(image.shape[1] * scale)), MIN_CELL_HEIGHT_PX),
            interpolation=cv2.INTER_CUBIC,
        )
    white: list[float] = [255.0] * (1 if image.ndim == 2 else 3)
    padded: np.ndarray = cv2.copyMakeBorder(
        image,
        CELL_BORDER_PX,
        CELL_BORDER_PX,
        CELL_BORDER_PX,
        CELL_BORDER_PX,
        cv2.BORDER_CONSTANT,
        value=white,
    )
    return padded


@dataclasses.dataclass(frozen=True)
class CellText:
    text: str
    confidence: float
    engine: str


class OcrEngine(Protocol):
    name: str

    def available(self) -> bool: ...

    def read(self, image: np.ndarray) -> CellText: ...


class TesseractEngine:
    """psm 7 -- a table cell is a single text line (§6)."""

    name = "tesseract"

    def __init__(self, config: str = "--psm 7") -> None:
        self._config = config
        self._checked: bool | None = None

    def available(self) -> bool:
        if self._checked is None:
            try:
                import pytesseract

                pytesseract.get_tesseract_version()
                self._checked = True
            except Exception as exc:  # noqa: BLE001 -- optional dependency probe
                logger.warning("tesseract_unavailable", error=str(exc))
                self._checked = False
        return self._checked

    def read(self, image: np.ndarray) -> CellText:
        import pytesseract

        data = pytesseract.image_to_data(
            image, config=self._config, output_type=pytesseract.Output.DICT
        )
        words: list[str] = []
        confidences: list[float] = []
        for text, confidence in zip(data["text"], data["conf"], strict=False):
            cleaned = text.strip()
            if not cleaned:
                continue
            words.append(cleaned)
            # tesseract reports -1 for non-text blocks
            confidences.append(max(0.0, float(confidence)) / 100.0)
        if not words:
            return CellText(text="", confidence=0.0, engine=self.name)
        return CellText(
            text=" ".join(words),
            confidence=sum(confidences) / len(confidences),
            engine=self.name,
        )


class PaddleEngine:
    """PP-OCRv4 with the angle classifier -- handles cells photographed
    rotated (§12). Imported lazily: it's a heavy dependency and its
    first load initializes models."""

    name = "paddle"

    def __init__(self) -> None:
        # Any: PaddleOCR's handle and result shapes differ across 2.x/3.x
        self._ocr: Any = None
        self._lock = threading.Lock()
        self._failed = False

    def available(self) -> bool:
        return not self._failed and self._load() is not None

    def _load(self) -> Any:
        if self._ocr is not None or self._failed:
            return self._ocr
        with self._lock:
            if self._ocr is not None or self._failed:
                return self._ocr
            try:
                from paddleocr import PaddleOCR

                try:
                    # 3.x: angle handling renamed, show_log removed
                    self._ocr = PaddleOCR(
                        lang="en",
                        use_textline_orientation=True,
                        use_doc_orientation_classify=False,
                        use_doc_unwarping=False,
                    )
                except (TypeError, ValueError):
                    self._ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
            except Exception as exc:  # noqa: BLE001 -- optional heavy dependency
                logger.warning("paddleocr_unavailable", error=str(exc))
                self._failed = True
        return self._ocr

    @staticmethod
    def _parse(result: Any) -> tuple[list[str], list[float]]:
        """Accepts both the 3.x dict-per-image and 2.x nested-list shapes."""
        texts: list[str] = []
        confidences: list[float] = []
        for item in result or []:
            if isinstance(item, dict) or hasattr(item, "get"):
                rec_texts = item.get("rec_texts") or []
                rec_scores = item.get("rec_scores") or []
                for text, score in zip(rec_texts, rec_scores, strict=False):
                    if str(text).strip():
                        texts.append(str(text).strip())
                        confidences.append(float(score))
                continue
            for line in item or []:
                if not line or len(line) < 2:
                    continue
                text, confidence = line[1][0], float(line[1][1])
                if str(text).strip():
                    texts.append(str(text).strip())
                    confidences.append(confidence)
        return texts, confidences

    def read(self, image: np.ndarray) -> CellText:
        engine = self._load()
        if engine is None:
            return CellText(text="", confidence=0.0, engine=self.name)
        try:
            if hasattr(engine, "predict"):
                result = engine.predict(image)
            else:
                result = engine.ocr(image, cls=True)
        except Exception as exc:  # noqa: BLE001 -- never let one cell kill a sheet
            logger.warning("paddleocr_cell_failed", error=str(exc))
            return CellText(text="", confidence=0.0, engine=self.name)
        texts, confidences = self._parse(result)
        if not texts:
            return CellText(text="", confidence=0.0, engine=self.name)
        return CellText(
            text=" ".join(texts),
            confidence=sum(confidences) / len(confidences),
            engine=self.name,
        )


def normalize_numeric(text: str) -> str:
    """Digit-confusion repair + separator normalization for numeric cells
    only (§6)."""
    repaired = _NON_NUMERIC.sub("", text.translate(_DIGIT_CONFUSIONS))
    if "," in repaired and "." in repaired:
        repaired = repaired.replace(",", "")  # thousands separator
    elif repaired.count(",") == 1 and len(repaired.split(",")[-1]) in {1, 2, 3}:
        repaired = repaired.replace(",", ".")  # decimal comma
    else:
        repaired = repaired.replace(",", "")
    if repaired.count(".") > 1:  # keep the last dot as the decimal point
        head, _, tail = repaired.rpartition(".")
        repaired = head.replace(".", "") + "." + tail
    return repaired.strip(".") if repaired in {".", ""} else repaired


class DualEngine:
    """Primary + fallback with a confidence-based handoff (§6)."""

    def __init__(
        self,
        primary: OcrEngine | None = None,
        fallback: OcrEngine | None = None,
        threshold: float = FALLBACK_THRESHOLD,
    ) -> None:
        self._primary = primary if primary is not None else PaddleEngine()
        self._fallback = fallback if fallback is not None else TesseractEngine()
        self._threshold = threshold

    @property
    def engines_available(self) -> list[str]:
        return [
            engine.name
            for engine in (self._primary, self._fallback)
            if engine is not None and engine.available()
        ]

    def read(self, image: np.ndarray, *, numeric: bool = False) -> CellText:
        image = prepare_cell(image)
        results: list[CellText] = []
        if self._primary is not None and self._primary.available():
            results.append(self._primary.read(image))
        best = results[0] if results else None
        if (best is None or best.confidence < self._threshold) and (
            self._fallback is not None and self._fallback.available()
        ):
            results.append(self._fallback.read(image))
            best = max(results, key=lambda r: r.confidence)
        if best is None:
            return CellText(text="", confidence=0.0, engine="none")
        if numeric:
            return dataclasses.replace(best, text=normalize_numeric(best.text))
        return best
