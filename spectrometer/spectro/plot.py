"""Headless spectrum plot (PNG export without a GUI)."""

from __future__ import annotations

import cv2
import numpy as np

from .calibration import Calibration, generate_graticule
from .color import wavelength_to_rgb
from .peaks import find_peaks
from .roi import Roi
from .spectrum import SpectrumResult

__all__ = ["render_spectrum"]

_BG = (12, 12, 16)
_GRID = (52, 52, 60)
_GRID_STRONG = (86, 86, 96)
_TEXT = (225, 225, 225)
_DIM = (145, 145, 155)
_ACCENT = (120, 220, 255)
_YELLOW = (70, 220, 255)


def _text(image: np.ndarray, text: str, org: tuple[int, int], scale: float = 0.42, color=_TEXT) -> None:
    cv2.putText(image, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def render_spectrum(
    result: SpectrumResult,
    *,
    calibration: Calibration | None = None,
    roi: Roi | None = None,
    width: int = 1280,
    height: int = 480,
    title: str = "Enzyflow spectrum",
    peak_threshold: float = 0.12,
    peak_min_distance: int = 12,
) -> np.ndarray:
    image = np.full((height, width, 3), _BG, dtype=np.uint8)
    left, right, top, bottom = 60, 24, 44, 48
    x0, y0 = left, top
    x1, y1 = width - right, height - bottom
    plot_w, plot_h = x1 - x0, y1 - y0
    values = np.clip(np.asarray(result.display, dtype=float), 0.0, None)
    if values.size == 0:
        _text(image, "no data", (x0 + 10, y0 + 20), 0.7, _DIM)
        return image
    vmax = max(1e-9, float(np.max(values)))
    quiet = vmax < 1.5 and result.raw_max < 10.0
    n = values.size
    wl = result.wavelengths

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = int(y1 - frac * plot_h)
        cv2.line(image, (x0, gy), (x1, gy), _GRID if frac not in (0.0, 1.0) else _GRID_STRONG, 1)
        _text(image, f"{vmax * frac:.0f}", (6, gy + 4), 0.32, _DIM)

    if calibration is not None and roi is not None:
        tens, fifties = generate_graticule(calibration, roi.x, roi.w)
        for index, _ in tens:
            gx = x0 + int(index * plot_w / max(1, n - 1))
            cv2.line(image, (gx, y0), (gx, y1), _GRID, 1)
        for index, wavelength in fifties:
            gx = x0 + int(index * plot_w / max(1, n - 1))
            cv2.line(image, (gx, y0), (gx, y1), _GRID_STRONG, 1)
            _text(image, f"{wavelength:.0f}nm", (gx - 18, y1 + 18), 0.32, _TEXT)
        _text(image, "wavelength (nm)", (x1 - 100, y1 + 36), 0.32, _DIM)
    else:
        _text(image, "pixel (uncalibrated)", (x1 - 120, y1 + 36), 0.32, _DIM)

    for gx in range(plot_w):
        index = int(round(gx * (n - 1) / max(1, plot_w - 1)))
        gy = int(y1 - (values[index] / vmax) * plot_h)
        if quiet:
            color = (105, 105, 105)
        elif wl is not None and wl.size > index:
            r, g, b = wavelength_to_rgb(float(wl[index]))
            color = (b, g, r)
        else:
            color = (200, 200, 200)
        cv2.line(image, (x0 + gx, y1), (x0 + gx, gy), color, 1)

    if n >= 3 and not quiet:
        peaks = find_peaks(values, threshold=peak_threshold, min_distance=peak_min_distance)
        for index in sorted(peaks.tolist(), key=lambda i: values[i], reverse=True)[:12]:
            gx = x0 + int(index * plot_w / max(1, n - 1))
            gy = int(y1 - (values[index] / vmax) * plot_h)
            label = f"{float(wl[index]):.1f}nm" if wl is not None else f"px{int(result.pixels[index])}"
            tx = min(max(x0 + 2, gx - 10), x1 - 75)
            ty = max(y0 + 14, gy - 8)
            cv2.rectangle(image, (tx - 2, ty - 11), (tx + 68, ty + 2), (28, 28, 34), -1)
            cv2.rectangle(image, (tx - 2, ty - 11), (tx + 68, ty + 2), _YELLOW, 1)
            _text(image, label, (tx + 2, ty - 1), 0.32, _YELLOW)

    _text(image, title, (x0, 24), 0.55, _ACCENT)
    cal_text = calibration.describe() if calibration else "UNCALIBRATED"
    _text(image, cal_text, (x0 + 150, 24), 0.36, _DIM)
    if quiet:
        _text(image, "LOW SIGNAL - open the slit or increase exposure", (x0 + 10, y0 + 24), 0.45, (90, 90, 255))
    else:
        _text(image, f"peak {result.peak_wavelength:.1f} nm" if result.peak_wavelength is not None else "", (x0, 38), 0.34, _DIM)
    return image
