"""Barcode decoding for the Assembly Scanner.

``decode(frame) -> list[str]`` returns deduplicated barcode text in first-seen order.
The resolver determines whether a decoded value matches the loaded BOM.

The decoder tries a small angle sweep and then a magnified centre crop to improve
readability when a label is not held perfectly level or close enough to the camera.

Early exit: ``stop(codes_so_far) -> bool`` is called after each pass; when it
returns True the remaining passes are skipped (the UI passes "resolve() gives
MATCH"). Without ``stop`` every pass runs and the union is returned.
"""
from __future__ import annotations

from typing import Callable, Optional

import cv2
import numpy as np
import zxingcpp

_F = zxingcpp.BarcodeFormat
# Code 128 only: the Mouser P/N is only ever in the Code 128 symbol (barcode handling). QR was
# removed after the live app hung inside ZXing QRCode::Reader::read on a webcam frame (7 Oct).
FORMATS = _F.Code128

ANGLES: tuple[float, ...] = (0.0, -4.0, 4.0, -8.0, 8.0)
ROI_FRACTION = 0.5     # centre ROI width/height as a fraction of the frame
ROI_UPSCALE = 2.0

StopFn = Callable[[list[str]], bool]


def roi_rect(width: int, height: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the centre ROI used by the upscale pass (also drawn in the UI)."""
    w = int(width * ROI_FRACTION)
    h = int(height * ROI_FRACTION)
    return (width - w) // 2, (height - h) // 2, w, h


def _gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _read(img: np.ndarray, out: list[str], seen: set[str]) -> None:
    for b in zxingcpp.read_barcodes(img, formats=FORMATS, try_invert=False):
        t = b.text
        if t and t not in seen:
            seen.add(t)
            out.append(t)


def _rotate(img: np.ndarray, deg: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def decode(frame: np.ndarray, *, stop: Optional[StopFn] = None,
           angles: tuple[float, ...] = ANGLES, roi_pass: bool = True) -> list[str]:
    """All decoded barcode texts in ``frame`` (BGR, BGRA or grayscale uint8)."""
    if frame is None or frame.size == 0:
        return []
    g = np.ascontiguousarray(_gray(frame))
    out: list[str] = []
    seen: set[str] = set()
    for a in angles:
        _read(g if a == 0 else _rotate(g, a), out, seen)
        if stop is not None and out and stop(out):
            return out
    if roi_pass:
        x, y, w, h = roi_rect(g.shape[1], g.shape[0])
        if w >= 16 and h >= 16:
            roi = cv2.resize(g[y:y + h, x:x + w], None, fx=ROI_UPSCALE, fy=ROI_UPSCALE,
                             interpolation=cv2.INTER_CUBIC)
            _read(roi, out, seen)
    return out


def load_image(path: str) -> np.ndarray:
    """Load a still (incl. HEIC) as a BGR array, EXIF orientation applied. Dev/test helper."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:  # pragma: no cover
        pass
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


if __name__ == "__main__":  # python -m app.scanner <image>...
    import sys
    import time
    for p in sys.argv[1:]:
        img = load_image(p)
        t0 = time.perf_counter()
        codes = decode(img)
        print(f"{p}: {(time.perf_counter() - t0) * 1000:.1f} ms")
        for c in codes:
            print("  ", repr(c))
