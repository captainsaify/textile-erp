"""Image preprocessing -- docs/07_OCR.md §2.

Raw phone photo / PDF page bytes in, normalized top-down grayscale sheet
out, plus a binarized view for geometry work. Each step is separately
callable so failures degrade gracefully: if crop can't find a sheet
boundary we keep the deskewed full frame rather than aborting.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np

MAX_DESKEW_DEGREES = 15.0
MIN_CROP_AREA_RATIO = 0.25  # a "sheet" contour smaller than this is noise


@dataclasses.dataclass(frozen=True)
class PreparedImage:
    gray: np.ndarray  # deskewed, denoised, cropped grayscale
    binary: np.ndarray  # adaptive-thresholded (ink = white) for geometry
    deskew_angle: float
    cropped: bool


def decode(data: bytes) -> np.ndarray:
    """Bytes -> BGR image. Raises ValueError on anything undecodable."""
    buffer = np.frombuffer(data, dtype=np.uint8)
    image: np.ndarray | None = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode image data")
    return image


def render_pdf_pages(data: bytes, dpi: int = 300) -> list[bytes]:
    """PDF -> one PNG per page at 300 DPI (§2 step 1). Requires poppler."""
    import io

    from pdf2image import convert_from_bytes

    pages = convert_from_bytes(data, dpi=dpi)
    rendered: list[bytes] = []
    for page in pages:
        buffer = io.BytesIO()
        page.save(buffer, format="PNG")
        rendered.append(buffer.getvalue())
    return rendered


def estimate_skew(binary: np.ndarray) -> float:
    """Correction angle in degrees, ready to hand to `rotate` (§2 step 2)."""
    coords = np.column_stack(np.where(binary > 0))
    if coords.shape[0] < 50:
        return 0.0
    # np.where gives (row, col); minAreaRect wants (x, y), and feeding it
    # the transposed pair negates the angle it reports.
    points = np.column_stack((coords[:, 1], coords[:, 0])).astype(np.float32)
    angle = float(cv2.minAreaRect(points)[-1])
    # OpenCV has reported this in both [-90, 0] and (0, 90] across
    # versions, and which of the two edges counts as "width" flips with
    # the ink block's aspect ratio. Folding into (-45, 45] makes every
    # convention land on the same small signed correction.
    angle = (angle + 45.0) % 90.0 - 45.0
    if abs(angle) > MAX_DESKEW_DEGREES:
        return 0.0
    return angle


def rotate(image: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.1:
        return image
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    rotated: np.ndarray = cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return rotated


def binarize(gray: np.ndarray) -> np.ndarray:
    """Adaptive threshold, ink=white (§2 step 4). Gaussian + a block size
    large enough to span a text line but small enough to track the
    lighting gradient across a hand-held photo."""
    thresholded: np.ndarray = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        blockSize=25,
        C=10,
    )
    return thresholded


def _order_corners(points: np.ndarray) -> np.ndarray:
    """Corners as top-left, top-right, bottom-right, bottom-left."""
    ordered = np.zeros((4, 2), dtype=np.float32)
    total = points.sum(axis=1)
    diff = np.diff(points, axis=1).ravel()
    ordered[0] = points[np.argmin(total)]
    ordered[2] = points[np.argmax(total)]
    ordered[1] = points[np.argmin(diff)]
    ordered[3] = points[np.argmax(diff)]
    return ordered


def find_sheet_quad(binary: np.ndarray) -> np.ndarray | None:
    """Largest 4-corner contour that plausibly is the sheet (§2 step 5)."""
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    frame_area = float(binary.shape[0] * binary.shape[1])
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area = cv2.contourArea(contour)
        if area / frame_area < MIN_CROP_AREA_RATIO:
            break
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) == 4:
            return _order_corners(approx.reshape(4, 2).astype(np.float32))
    return None


def warp_to_quad(gray: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Perspective-correct the sheet to a flat top-down rectangle."""
    top_left, top_right, bottom_right, bottom_left = quad
    width = int(
        max(np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left))
    )
    height = int(
        max(np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right))
    )
    if width < 50 or height < 50:
        return gray
    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32
    )
    matrix = cv2.getPerspectiveTransform(quad, destination)
    warped: np.ndarray = cv2.warpPerspective(gray, matrix, (width, height), flags=cv2.INTER_CUBIC)
    return warped


def prepare(data: bytes, *, denoise: bool = True) -> PreparedImage:
    """Full §2 pipeline: grayscale -> deskew -> denoise -> crop -> binarize."""
    gray = cv2.cvtColor(decode(data), cv2.COLOR_BGR2GRAY)

    angle = estimate_skew(binarize(gray))
    gray = rotate(gray, angle)

    if denoise:
        # light only: aggressive denoising eats thin gridlines and small
        # digits (§2 step 3)
        gray = cv2.fastNlMeansDenoising(gray, h=7, templateWindowSize=7, searchWindowSize=21)

    quad = find_sheet_quad(binarize(gray))
    cropped = False
    if quad is not None:
        warped = warp_to_quad(gray, quad)
        if warped.shape[0] >= 50 and warped.shape[1] >= 50:
            gray = warped
            cropped = True

    return PreparedImage(gray=gray, binary=binarize(gray), deskew_angle=angle, cropped=cropped)
