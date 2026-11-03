"""Wavelength -> display colour conversion.

Ported from ``specFunctions.wavelength_to_rgb`` in PySpectrometer2
(Les Wright, Apache-2.0), which in turn follows Chris Webb's article
"Exploring the visible spectrum in Python".

This is a *display* approximation.  It is not a colorimetric conversion and
must not be used to claim calibrated sRGB values.
"""

from __future__ import annotations

import numpy as np

__all__ = ["wavelength_to_rgb"]


def wavelength_to_rgb(nm: float) -> tuple[int, int, int]:
    """Return an approximate display RGB triple for a wavelength in nm.

    Values outside 380..780 nm return neutral grey, matching the upstream
    behaviour (so an uncalibrated axis still draws visibly).
    """
    nm = float(nm)
    gamma = 0.8
    max_intensity = 255
    factor = 0.0
    r = g = b = 0.0

    if 380 <= nm <= 439:
        r = -(nm - 440) / (440 - 380)
        g = 0.0
        b = 1.0
    elif 440 <= nm <= 489:
        r = 0.0
        g = (nm - 440) / (490 - 440)
        b = 1.0
    elif 490 <= nm <= 509:
        r = 0.0
        g = 1.0
        b = -(nm - 510) / (510 - 490)
    elif 510 <= nm <= 579:
        r = (nm - 510) / (580 - 510)
        g = 1.0
        b = 0.0
    elif 580 <= nm <= 644:
        r = 1.0
        g = -(nm - 645) / (645 - 580)
        b = 0.0
    elif 645 <= nm <= 780:
        r = 1.0
        g = 0.0
        b = 0.0

    if 380 <= nm <= 419:
        factor = 0.3 + 0.7 * (nm - 380) / (420 - 380)
    elif 420 <= nm <= 700:
        factor = 1.0
    elif 701 <= nm <= 780:
        factor = 0.3 + 0.7 * (780 - nm) / (780 - 700)

    def _scale(value: float) -> int:
        if value <= 0:
            return 0
        return int(max_intensity * ((value * factor) ** gamma))

    red, green, blue = _scale(r), _scale(g), _scale(b)
    if red + green + blue == 0:
        # Display no colour as grey; keeps the graph readable when the
        # calibration lies outside the visible range.
        red = green = blue = 155
    return red, green, blue


def wavelength_colors(nm: np.ndarray) -> np.ndarray:
    """Vectorised helper returning an (N, 3) uint8 RGB array."""
    nm = np.asarray(nm, dtype=float)
    out = np.empty((nm.size, 3), dtype=np.uint8)
    for i, value in enumerate(nm.ravel()):
        out[i] = wavelength_to_rgb(value)
    return out.reshape(nm.shape + (3,))
