"""Peak finding and smoothing helpers.

The Savitzky-Golay implementation is a NumPy-only rewrite of the public
domain / BSD-licensed SciPy cookbook recipe used by PySpectrometer2.
The peak finder is a small, predictable local-maximum implementation
inspired by ``peakutils.peak.indexes`` (MIT, Lucas Hermann Negri), which is
also what PySpectrometer2 uses.
"""

from __future__ import annotations

from math import factorial

import numpy as np

__all__ = [
    "savitzky_golay",
    "find_peaks",
    "find_peaks_dips",
]


def savitzky_golay(
    y: np.ndarray,
    window_size: int = 9,
    order: int = 3,
    deriv: int = 0,
    rate: float = 1.0,
) -> np.ndarray:
    """Savitzky-Golay filter without SciPy.

    Parameters are the same as the classic ``savgol_filter`` recipe:
    ``window_size`` must be a positive odd integer and larger than ``order``.
    If the signal is shorter than the window, a copy is returned unchanged.
    """
    y = np.asarray(y, dtype=float)
    if y.ndim != 1:
        raise ValueError("y must be one-dimensional")

    window_size = int(abs(window_size))
    order = int(abs(order))
    if window_size % 2 != 1 or window_size < 1:
        raise ValueError("window_size must be a positive odd number")
    if window_size < order + 2:
        raise ValueError("window_size is too small for the polynomial order")
    if y.size < window_size:
        return y.copy()

    half_window = (window_size - 1) // 2
    order_range = range(order + 1)
    b = np.asarray(
        [[k**i for i in order_range] for k in range(-half_window, half_window + 1)],
        dtype=float,
    )
    # np.linalg.pinv(...) replaces the removed np.mat(...).A idiom.
    m = np.linalg.pinv(b)[deriv] * rate**deriv * factorial(deriv)

    firstvals = y[0] - np.abs(y[1 : half_window + 1][::-1] - y[0])
    lastvals = y[-1] + np.abs(y[-half_window - 1 : -1][::-1] - y[-1])
    padded = np.concatenate((firstvals, y, lastvals))
    return np.convolve(m[::-1], padded, mode="valid")


def find_peaks(
    y: np.ndarray,
    threshold: float = 0.2,
    min_distance: int = 10,
    threshold_abs: bool = False,
) -> np.ndarray:
    """Return indices of local maxima, strongest first, with suppression.

    ``threshold`` is relative to ``max(y) - min(y)`` unless ``threshold_abs``
    is true.  ``min_distance`` is expressed in samples; when peaks are closer
    than this, only the strongest is kept.  The returned indices are sorted
    in ascending order for convenient plotting/labelling.
    """
    y = np.asarray(y, dtype=float).ravel()
    if y.size == 0:
        return np.empty(0, dtype=int)
    if not np.all(np.isfinite(y)):
        y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)

    if threshold_abs:
        level = float(threshold)
    else:
        level = float(np.min(y) + threshold * (np.max(y) - np.min(y)))

    if y.size < 3:
        return np.arange(y.size) if np.max(y) > level else np.empty(0, dtype=int)

    left = np.empty_like(y)
    right = np.empty_like(y)
    left[0] = -np.inf
    left[1:] = y[:-1]
    right[-1] = -np.inf
    right[:-1] = y[1:]

    # >= on the right makes the last sample of a flat plateau the peak.
    candidates = np.where((y > left) & (y >= right) & (y > level))[0]
    if candidates.size <= 1 or min_distance <= 1:
        return candidates.astype(int)

    order = candidates[np.argsort(y[candidates])[::-1]]
    keep = np.ones(y.size, dtype=bool)
    selected: list[int] = []
    for peak in order:
        if not keep[peak]:
            continue
        selected.append(int(peak))
        lo = max(0, int(peak) - int(min_distance))
        hi = min(y.size - 1, int(peak) + int(min_distance))
        keep[lo : hi + 1] = False
        keep[peak] = True
        # Marking the whole neighbourhood false is intentional for the
        # next candidates; the chosen peak itself does not need re-checking.
    return np.asarray(sorted(selected), dtype=int)


def find_peaks_dips(
    y: np.ndarray,
    window: int = 5,
    unique_window: float = 50.0,
    wavelength_axis: np.ndarray | None = None,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Find peaks and dips using a local-window comparison.

    This mirrors the simple approach in spectro-cam-rs: a sample is a peak if
    it is greater than every sample in a +/- ``window`` neighbourhood, and a
    dip if it is lower than every neighbour.  Near-duplicates within
    ``unique_window`` (in nm when ``wavelength_axis`` is supplied, otherwise in
    samples) are reduced to the strongest one.
    """
    y = np.asarray(y, dtype=float).ravel()
    n = y.size
    window = max(1, int(window))
    peaks: list[int] = []
    dips: list[int] = []
    for i in range(window, n - window):
        neighbourhood = np.concatenate((y[i - window : i], y[i + 1 : i + window + 1]))
        if np.all(y[i] > neighbourhood):
            peaks.append(i)
        elif np.all(y[i] < neighbourhood):
            dips.append(i)

    def _unique(indexes: list[int], highest: bool) -> list[tuple[int, float]]:
        result: list[tuple[int, float]] = []
        for idx in indexes:
            coord = float(wavelength_axis[idx]) if wavelength_axis is not None else float(idx)
            same = [
                other
                for other in indexes
                if abs(
                    (float(wavelength_axis[other]) if wavelength_axis is not None else float(other))
                    - coord
                )
                <= unique_window / 2.0
            ]
            best = (max if highest else min)(same, key=lambda j: y[j])
            if idx == best and (idx, float(y[idx])) not in result:
                result.append((int(idx), float(y[idx])))
        return result

    return _unique(peaks, True), _unique(dips, False)
