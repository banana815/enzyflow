"""Black-body tungsten reference spectrum.

Ported from ``src/tungsten_halogen.rs`` in spectro-cam-rs (DerFetzer,
Apache-2.0 / MIT), which implements the emissivity model from

  J. H. de Vos et al., "A new method for the determination of the emissivity
  of tungsten", Applied Optics 23 (1984) 975.

and the spectral irradiance relation from Planck's law.  The result is a
relative reference curve normalised to its maximum, suitable for the
"set reference as calibration" workflow.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

__all__ = ["tungsten_reference", "save_tungsten_csv"]

# CODATA 2018 values, enough precision for a relative reference curve.
_C = 299_792_458.0
_H = 6.626_070_15e-34
_K = 1.380_649e-23
_T0 = 2.200


def _emissivity(wavelength_nm: float, filament_temp: float) -> float | None:
    t = filament_temp / 1000.0
    w = wavelength_nm
    if w < 340:
        return None
    if w < 420:
        l0, a0, a1, b0, b1, b2, c0, c1 = 0.380, 0.47245, -0.0155, -0.0086, -0.0229, 0.0, -2.86, 0.0
    elif w < 480:
        l0, a0, a1, b0, b1, b2, c0, c1 = 0.450, 0.46361, -0.0172, -0.1304, 0.0, 0.0, 0.52, 0.0
    elif w < 580:
        l0, a0, a1, b0, b1, b2, c0, c1 = 0.530, 0.45549, -0.0173, -0.1150, 0.0, 0.0, -0.5, 0.0
    elif w < 640:
        l0, a0, a1, b0, b1, b2, c0, c1 = 0.610, 0.44297, -0.0177, -0.1482, 0.0, 0.0, 0.723, 0.0
    elif w < 760:
        l0, a0, a1, b0, b1, b2, c0, c1 = 0.700, 0.43151, -0.0207, -0.1441, -0.0551, 0.0, -0.278, -0.190
    elif w < 940:
        l0, a0, a1, b0, b1, b2, c0, c1 = 0.850, 0.40610, -0.0259, -0.1889, 0.0087, 0.0290, -0.126, 0.246
    elif w < 1600:
        l0, a0, a1, b0, b1, b2, c0, c1 = 1.270, 0.32835, 0.0, -0.1686, 0.0737, 0.0, 0.046, 0.016
    elif w <= 2600:
        l0, a0, a1, b0, b1, b2, c0, c1 = 2.100, 0.22631, 0.0431, -0.0829, 0.0241, 0.0, 0.04, -0.026
    else:
        return None
    d = t - _T0
    x = w / 1000.0 - l0
    return a0 + a1 * d + (b0 + b1 * d + b2 * d * d) * x + (c0 + c1 * d) * x * x


def _spectral_irradiance(wavelength_nm: float, filament_temp: float) -> float | None:
    e = _emissivity(wavelength_nm, filament_temp)
    if e is None:
        return None
    lam = wavelength_nm * 1e-9
    return e * 2.0 * _H * _C**2 / (lam**5 * np.expm1(_H * _C / (lam * _K * filament_temp)))


def tungsten_reference(
    filament_temp: int = 2800,
    low_nm: int = 340,
    high_nm: int = 2000,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (wavelength_nm, relative_intensity) normalised to max=1."""
    if not (1000 <= int(filament_temp) <= 4000):
        raise ValueError("filament temperature must be between 1000 and 4000 K")
    wavelengths = np.arange(int(low_nm), int(high_nm), dtype=float)
    values = np.asarray(
        [_spectral_irradiance(float(w), float(filament_temp)) or 0.0 for w in wavelengths],
        dtype=float,
    )
    maximum = float(np.max(values)) if values.size else 0.0
    if maximum <= 0:
        raise ValueError("could not generate a tungsten reference for this range")
    return wavelengths, values / maximum


def save_tungsten_csv(path: str | Path, filament_temp: int = 2800, low_nm: int = 340, high_nm: int = 2000) -> Path:
    wavelengths, values = tungsten_reference(filament_temp, low_nm, high_nm)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["wavelength_nm", "intensity"])
        for wavelength, value in zip(wavelengths, values):
            writer.writerow([f"{wavelength:.0f}", f"{value:.9g}"])
    return path
