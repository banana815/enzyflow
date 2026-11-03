"""Pixel -> wavelength calibration.

The approach follows PySpectrometer2: at least three known wavelength/pixel
pairs are fitted with a 2nd-order polynomial; four or more points use a
3rd-order polynomial.  Calibration points are stored in *absolute* frame
pixel coordinates so moving the ROI horizontally does not invalidate the
calibration.

spectro-cam-rs uses a simpler two-point linear mapping.  This module keeps
the polynomial model because it is more accurate on a flat camera sensor and
is what most of the Raspberry Pi spectrometers in the field use.
"""

from __future__ import annotations

import hashlib
import json
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

__all__ = ["Calibration", "CalibrationError", "generate_graticule"]


class CalibrationError(ValueError):
    """Raised when calibration points cannot produce a sane wavelength axis."""


def _degree_name(degree: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(degree, f"{degree}th")


@dataclass
class Calibration:
    points: list[tuple[int, float]]
    coefficients: list[float]
    degree: int
    r_squared: float | None = None
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    note: str = ""
    frame_width: int | None = None

    # ------------------------------------------------------------------ fit
    @classmethod
    def fit(
        cls,
        points: Iterable[Sequence[float] | tuple[float, float]],
        degree: int | None = None,
        *,
        note: str = "",
        frame_width: int | None = None,
    ) -> "Calibration":
        cleaned: list[tuple[int, float]] = []
        for point in points:
            if len(point) != 2:
                raise CalibrationError("each calibration point must be (pixel, wavelength_nm)")
            pixel, wavelength = point
            pixel_i = int(round(float(pixel)))
            wavelength_f = float(wavelength)
            if pixel_i < 0:
                raise CalibrationError("pixel positions must be >= 0")
            if not (100.0 <= wavelength_f <= 3000.0):
                raise CalibrationError(
                    f"wavelength {wavelength_f} nm is outside the accepted 100..3000 nm range"
                )
            cleaned.append((pixel_i, wavelength_f))

        # Duplicate pixels make the fit singular; keep the last value but warn.
        by_pixel: dict[int, float] = {}
        for pixel, wavelength in cleaned:
            by_pixel[pixel] = wavelength
        cleaned = sorted(by_pixel.items())
        if len(cleaned) < 3:
            raise CalibrationError("at least 3 calibration points are required")

        if degree is None:
            degree = 2 if len(cleaned) == 3 else 3
        degree = int(degree)
        if degree < 1:
            raise CalibrationError("polynomial degree must be >= 1")
        degree = min(degree, len(cleaned) - 1)

        pixels = np.asarray([p for p, _ in cleaned], dtype=float)
        wavelengths = np.asarray([w for _, w in cleaned], dtype=float)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                coefficients = np.polyfit(pixels, wavelengths, degree)
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                raise CalibrationError(f"polynomial fit failed: {exc}") from exc
        if any("RankWarning" in str(item.category.__name__) for item in caught):
            raise CalibrationError("calibration points are ill-conditioned; use distinct pixels")
        if not np.all(np.isfinite(coefficients)):
            raise CalibrationError("polynomial fit produced non-finite coefficients")

        predicted = np.polyval(coefficients, pixels)
        residuals = wavelengths - predicted
        ss_res = float(np.sum(residuals**2))
        ss_tot = float(np.sum((wavelengths - np.mean(wavelengths)) ** 2))
        r_squared = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot

        return cls(
            points=[(int(p), float(w)) for p, w in cleaned],
            coefficients=[float(c) for c in coefficients],
            degree=degree,
            r_squared=float(r_squared),
            note=note,
            frame_width=frame_width,
        )

    # ------------------------------------------------------------- evaluate
    def wavelength_at(self, pixel: float) -> float:
        return float(np.polyval(np.asarray(self.coefficients, dtype=float), float(pixel)))

    def wavelengths(self, pixel_start: int, width: int) -> np.ndarray:
        pixels = np.arange(int(pixel_start), int(pixel_start) + int(width), dtype=float)
        return np.polyval(np.asarray(self.coefficients, dtype=float), pixels)

    def pixel_for_wavelength(
        self, wavelength_nm: float, pixel_start: int, width: int
    ) -> int | None:
        """Return the nearest pixel index (absolute) for a wavelength."""
        axis = self.wavelengths(pixel_start, width)
        if axis.size == 0:
            return None
        index = int(np.argmin(np.abs(axis - float(wavelength_nm))))
        if abs(float(axis[index]) - float(wavelength_nm)) > 2.0:
            return None
        return int(pixel_start) + index

    def points_in_roi(self, pixel_start: int, width: int) -> int:
        return sum(1 for pixel, _ in self.points if pixel_start <= pixel < pixel_start + width)

    def describe(self) -> str:
        if self.r_squared is None:
            quality = ""
        else:
            quality = f", R2={self.r_squared:.6f}"
        return f"{len(self.points)} pt, {_degree_name(self.degree)} order{quality}"

    @property
    def calibration_id(self) -> str:
        payload = json.dumps(
            {"points": self.points, "degree": self.degree},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha1(payload).hexdigest()[:12]

    # ------------------------------------------------------------ (de)serial
    def to_dict(self) -> dict:
        return {
            "schema": 1,
            "type": "wavelength_calibration",
            "created": self.created,
            "note": self.note,
            "frame_width": self.frame_width,
            "points": [[int(p), float(w)] for p, w in self.points],
            "degree": int(self.degree),
            "coefficients": [float(c) for c in self.coefficients],
            "r_squared": self.r_squared,
            "calibration_id": self.calibration_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Calibration":
        if "points" not in data or "coefficients" not in data:
            raise CalibrationError("not a calibration JSON object")
        points = [(int(p), float(w)) for p, w in data["points"]]
        coefficients = [float(c) for c in data["coefficients"]]
        if len(points) < 3 or not coefficients:
            raise CalibrationError("calibration record is incomplete")
        return cls(
            points=points,
            coefficients=coefficients,
            degree=int(data.get("degree", len(coefficients) - 1)),
            r_squared=float(data["r_squared"]) if data.get("r_squared") is not None else None,
            created=str(data.get("created", "")),
            note=str(data.get("note", "")),
            frame_width=int(data["frame_width"]) if data.get("frame_width") is not None else None,
        )

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Calibration":
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        stripped = text.lstrip()
        if stripped.startswith("{"):
            return cls.from_dict(json.loads(text))
        return cls.from_caldata_text(text, source=path.name)

    # ------------------------------------------------ PySpectrometer2 compat
    @classmethod
    def from_caldata_text(cls, text: str, *, source: str = "caldata.txt", roi_x: int = 0) -> "Calibration":
        """Read PySpectrometer2's two-line ``caldata.txt`` format.

        The file contains pixel positions on the first line and the matching
        wavelengths on the second, comma separated.  Pixels are relative to
        the ROI/capture width, so ``roi_x`` can be added when importing from a
        different ROI origin.
        """
        lines = [line.strip() for line in text.replace("\r", "").splitlines() if line.strip()]
        if len(lines) < 2:
            raise CalibrationError(f"{source}: expected two non-empty lines")
        try:
            pixels = [int(round(float(x))) + int(roi_x) for x in lines[0].split(",") if x.strip()]
            wavelengths = [float(x) for x in lines[1].split(",") if x.strip()]
        except ValueError as exc:
            raise CalibrationError(f"{source}: could not parse numbers ({exc})") from exc
        if len(pixels) != len(wavelengths):
            raise CalibrationError(f"{source}: pixel and wavelength counts differ")
        return cls.fit(zip(pixels, wavelengths), note=f"imported from {source}")

    @classmethod
    def from_caldata_file(cls, path: str | Path, *, roi_x: int = 0) -> "Calibration":
        path = Path(path)
        return cls.from_caldata_text(path.read_text(encoding="utf-8"), source=path.name, roi_x=roi_x)

    def to_caldata_text(self, *, roi_x: int = 0) -> str:
        pixels = [pixel - int(roi_x) for pixel, _ in self.points]
        wavelengths = [wavelength for _, wavelength in self.points]
        return ",".join(str(p) for p in pixels) + "\n" + ",".join(f"{w:g}" for w in wavelengths) + "\n"

    def save_caldata(self, path: str | Path, *, roi_x: int = 0) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_caldata_text(roi_x=roi_x), encoding="utf-8")
        return path


def generate_graticule(
    calibration: Calibration,
    pixel_start: int,
    width: int,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (10 nm ticks, 50 nm labelled ticks) as (x_index, wavelength)."""
    axis = calibration.wavelengths(pixel_start, width)
    if axis.size == 0:
        return [], []
    low = int(np.floor(np.min(axis) / 10.0) * 10)
    high = int(np.ceil(np.max(axis) / 10.0) * 10)
    tens: list[tuple[int, float]] = []
    fifties: list[tuple[int, float]] = []
    for target in range(low, high + 1, 10):
        index = int(np.argmin(np.abs(axis - target)))
        if abs(float(axis[index]) - target) <= 0.75:
            tens.append((index, float(target)))
            if target % 50 == 0:
                fifties.append((index, float(target)))
    return tens, fifties
