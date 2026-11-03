"""Region-of-interest helpers.

The spectrum is extracted from a narrow horizontal band of the camera image.
The band can be selected with the mouse in the GUI, specified as
``x,y,w,h`` on the command line, or detected automatically.

Auto-detection is deliberately conservative: it looks for the brightest
horizontal band, expands it to include the illuminated rows, and trims empty
columns.  On a completely dark frame it falls back to a centred band so the
user still gets a visible ROI to adjust.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

__all__ = ["Roi", "normalise_roi", "auto_roi", "parse_roi"]


@dataclass
class Roi:
    x: int
    y: int
    w: int
    h: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    def crop(self, frame: np.ndarray) -> np.ndarray:
        return frame[self.y : self.y + self.h, self.x : self.x + self.w]

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


def normalise_roi(roi: Roi, frame_width: int, frame_height: int) -> Roi:
    x = max(0, min(int(roi.x), frame_width - 1))
    y = max(0, min(int(roi.y), frame_height - 1))
    w = max(1, min(int(roi.w) if roi.w else frame_width - x, frame_width - x))
    h = max(1, min(int(roi.h) if roi.h else 1, frame_height - y))
    return Roi(x, y, w, h)


def parse_roi(text: str, frame_width: int, frame_height: int) -> Roi:
    parts = [p.strip() for p in text.replace(";", ",").split(",")]
    if len(parts) != 4:
        raise ValueError("ROI must be x,y,w,h")
    try:
        values = [int(round(float(p))) for p in parts]
    except ValueError as exc:
        raise ValueError("ROI values must be integers") from exc
    return normalise_roi(Roi(*values), frame_width, frame_height)


def _smooth(values: np.ndarray, width: int = 5) -> np.ndarray:
    if values.size < width:
        return values
    kernel = np.ones(width, dtype=float) / width
    return np.convolve(values, kernel, mode="same")


def auto_roi(frame: np.ndarray, default_height: int = 8) -> Roi:
    """Detect a horizontal spectrum band and return an absolute ROI."""
    if frame is None or frame.size == 0:
        raise ValueError("empty frame")
    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame.astype(np.uint8)
    row_mean = _smooth(gray.mean(axis=1), 5)
    peak_row = int(np.argmax(row_mean))
    peak_value = float(row_mean[peak_row])

    if peak_value < 2.0:
        # Dark frame: centre a narrow band and let the user adjust it.
        h = max(1, min(int(default_height), height))
        y = max(0, (height - h) // 2)
        return Roi(0, y, width, h)

    threshold = max(2.0, 0.18 * peak_value)
    start = peak_row
    while start > 0 and row_mean[start - 1] >= threshold:
        start -= 1
    end = peak_row
    while end < height - 1 and row_mean[end + 1] >= threshold:
        end += 1

    # Include a little context around the bright band, but keep it narrow.
    pad = max(1, min(4, int(round((end - start + 1) * 0.25))))
    start = max(0, start - pad)
    end = min(height - 1, end + pad)
    h = max(1, end - start + 1)

    band = gray[start : end + 1, :]
    column_max = band.max(axis=0).astype(float)
    column_level = max(2.0, 0.10 * float(column_max.max()))
    active = np.where(column_max >= column_level)[0]
    if active.size >= 8:
        margin = max(2, int(round(width * 0.005)))
        x = max(0, int(active[0]) - margin)
        right = min(width - 1, int(active[-1]) + margin)
        w = max(8, right - x + 1)
    else:
        x, w = 0, width

    return Roi(x, start, w, h)
