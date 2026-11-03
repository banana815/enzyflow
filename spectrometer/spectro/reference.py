"""Reference-spectrum correction (relative response calibration).

Implements the idea used by spectro-cam-rs: compare the measured spectrum
against a known reference (for example a tungsten lamp or an imported CSV),
then multiply each wavelength bin by ``reference / measured``.  This corrects
the *shape* of the camera/grating response; it does not turn the camera into
an absolute radiometer.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

__all__ = [
    "load_reference_csv",
    "interpolate_reference",
    "scaling_factors",
    "apply_scaling",
]


def load_reference_csv(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Load a two-column reference CSV (wavelength_nm, intensity)."""
    wavelengths: list[float] = []
    values: list[float] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row or len(row) < 2:
                continue
            try:
                wavelength = float(row[0])
                value = float(row[1])
            except ValueError:
                # Header or comment line.
                continue
            if np.isfinite(wavelength) and np.isfinite(value):
                wavelengths.append(wavelength)
                values.append(value)
    if len(wavelengths) < 3:
        raise ValueError(f"{path}: expected at least 3 numeric wavelength/intensity rows")
    order = np.argsort(wavelengths)
    return np.asarray(wavelengths, dtype=float)[order], np.asarray(values, dtype=float)[order]


def interpolate_reference(
    reference_wavelengths: np.ndarray,
    reference_values: np.ndarray,
    wavelengths: np.ndarray,
    *,
    outside_value: float = 0.0,
) -> np.ndarray:
    """Linearly interpolate a reference onto the measured wavelength axis."""
    ref_w = np.asarray(reference_wavelengths, dtype=float)
    ref_v = np.asarray(reference_values, dtype=float)
    if ref_w.size < 2:
        raise ValueError("reference needs at least two points")
    order = np.argsort(ref_w)
    ref_w, ref_v = ref_w[order], ref_v[order]
    result = np.interp(wavelengths, ref_w, ref_v, left=outside_value, right=outside_value)
    return result


def scaling_factors(
    wavelengths: np.ndarray,
    measured: np.ndarray,
    reference_wavelengths: np.ndarray,
    reference_values: np.ndarray,
    *,
    floor: float = 0.05,
    clamp: tuple[float, float] = (0.05, 20.0),
) -> np.ndarray:
    """Return per-bin multipliers ``reference / measured`` with safeguards."""
    reference = interpolate_reference(reference_wavelengths, reference_values, wavelengths)
    measured = np.asarray(measured, dtype=float)
    denominator = np.maximum(measured, float(floor))
    factors = reference / denominator
    return np.clip(factors, clamp[0], clamp[1])


def apply_scaling(values: np.ndarray, factors: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    factors = np.asarray(factors, dtype=float)
    if values.shape != factors.shape:
        raise ValueError("scaling factors must match the spectrum shape")
    return values * factors
