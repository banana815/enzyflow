"""Spectrum extraction and processing.

Per frame:

1. average the RGB (and luminance) of every column inside the ROI;
2. optionally linearise the camera response (sRGB / Rec.601 / Rec.709);
3. average the last N frames (the "buffer" from spectro-cam-rs);
4. apply per-channel gains;
5. combine to an intensity curve;
6. subtract an optional dark profile, then an optional zero reference;
7. optionally hold peaks and apply a Savitzky-Golay display filter.

raw_intensity is the combined, gain-corrected camera count before dark/zero
subtraction. intensity retains signed corrected values; smoothing and peak
hold affect display/peak labels only.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

from .config import ProcessingConfig
from .peaks import savitzky_golay

__all__ = ["SpectrumResult", "SpectrumProcessor", "linearise"]


def linearise(values: np.ndarray, mode: str) -> np.ndarray:
    """Apply an inverse display gamma to normalised 0..255 camera values."""
    mode = (mode or "off").lower()
    if mode == "off":
        return values
    x = np.clip(np.asarray(values, dtype=float), 0.0, 255.0) / 255.0
    if mode in {"rec601", "rec709"}:
        y = np.where(x < 0.081, x / 4.5, ((x + 0.099) / 1.099) ** (1.0 / 0.45))
    elif mode == "srgb":
        y = np.where(x < 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    else:
        raise ValueError(f"unknown linearisation mode: {mode}")
    return np.clip(y, 0.0, 1.0) * 255.0


@dataclass
class SpectrumResult:
    pixels: np.ndarray
    r: np.ndarray
    g: np.ndarray
    b: np.ndarray
    luma: np.ndarray
    intensity: np.ndarray
    display: np.ndarray
    wavelengths: np.ndarray | None
    saturated: bool
    saturated_ratio: float
    raw_max: float
    buffer_len: int
    zero_active: bool
    hold_active: bool
    raw_intensity: np.ndarray | None = None
    dark_profile: np.ndarray | None = None
    dark_metadata: dict | None = None

    @property
    def peak_index(self) -> int:
        if self.intensity.size == 0:
            return 0
        return int(np.argmax(self.intensity))

    @property
    def peak_wavelength(self) -> float | None:
        if self.wavelengths is None or self.wavelengths.size == 0:
            return None
        return float(self.wavelengths[self.peak_index])

    def summary(self) -> dict:
        return {
            "width": int(self.intensity.size),
            "pixel_start": int(self.pixels[0]) if self.pixels.size else 0,
            "intensity_min": float(np.min(self.intensity)) if self.intensity.size else 0.0,
            "intensity_max": float(np.max(self.intensity)) if self.intensity.size else 0.0,
            "peak_pixel": int(self.pixels[self.peak_index]) if self.pixels.size else None,
            "peak_wavelength_nm": self.peak_wavelength,
            "saturated": bool(self.saturated),
            "saturated_ratio": float(self.saturated_ratio),
            "buffer_len": int(self.buffer_len),
            "zero_reference": bool(self.zero_active),
            "hold_peaks": bool(self.hold_active),
            "dark_corrected": self.dark_profile is not None,
        }


class SpectrumProcessor:
    def __init__(self, config: ProcessingConfig) -> None:
        self.config = config
        self._buffer: deque[np.ndarray] = deque(maxlen=max(1, int(config.buffer_size)))
        self._width: int | None = None
        self._zero: np.ndarray | None = None
        self._hold: np.ndarray | None = None
        self._last_raw: np.ndarray | None = None
        self._dark: np.ndarray | None = None
        self._dark_metadata: dict | None = None
        self._dark_pixel_start: int | None = None

    # ------------------------------------------------------------- lifecycle
    def set_config(self, config: ProcessingConfig) -> None:
        self.config = config
        if self._buffer.maxlen != max(1, int(config.buffer_size)):
            old = list(self._buffer)
            self._buffer = deque(old[-max(1, int(config.buffer_size)) :], maxlen=max(1, int(config.buffer_size)))

    def clear_buffer(self) -> None:
        self._buffer.clear()
        self._hold = None

    def reset(self) -> None:
        self.clear_buffer()
        self._zero = None
        self._last_raw = None
        self._width = None
        self._dark = self._dark_metadata = self._dark_pixel_start = None

    def set_dark_reference(self, profile, metadata, pixel_start):
        import copy
        profile = np.asarray(profile, dtype=np.float32)
        if profile.ndim != 1 or not profile.size or not np.all(np.isfinite(profile)):
            raise ValueError("invalid dark profile")
        self.reset()
        self._dark = profile.copy()
        self._dark_metadata = copy.deepcopy(metadata)
        self._dark_pixel_start = int(pixel_start)

    def clear_dark_reference(self):
        self.reset()

    def set_hold(self, enabled: bool) -> None:
        self.config.hold_peaks = bool(enabled)
        if not enabled:
            self._hold = None

    def set_savgol_enabled(self, enabled: bool) -> None:
        self.config.savgol_enabled = bool(enabled)

    def set_savgol_window(self, window: int) -> None:
        window = max(3, min(41, int(window) | 1))
        if window <= int(self.config.savgol_order):
            window = int(self.config.savgol_order) + 2 | 1
        self.config.savgol_window = window

    def set_zero_reference(self) -> bool:
        if self._last_raw is None:
            return False
        self._zero = self._last_raw.copy()
        self._hold = None
        return True

    def clear_zero_reference(self) -> None:
        self._zero = None
        self._hold = None

    @property
    def zero_active(self) -> bool:
        return self._zero is not None

    # -------------------------------------------------------------- extraction
    @staticmethod
    def _extract(crop: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        if crop is None or crop.size == 0:
            raise ValueError("empty ROI")
        if crop.ndim == 2:
            crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
        if crop.ndim != 3 or crop.shape[2] < 3:
            raise ValueError("ROI must be an 8-bit BGR or grayscale image")
        # BGR means over the ROI rows -> one value per column.
        mean = crop.astype(np.float32).mean(axis=0)
        b = mean[:, 0]
        g = mean[:, 1]
        r = mean[:, 2]
        luma = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32).mean(axis=0)
        return r, g, b, luma

    def update(self, crop: np.ndarray, pixel_start: int = 0) -> SpectrumResult:
        r, g, b, luma = self._extract(crop)
        width = int(r.size)
        if self._dark is not None and (self._dark.size != width or self._dark_pixel_start != pixel_start):
            self.reset()
        if self._width is not None and width != self._width:
            # A different ROI width invalidates the buffer and zero reference.
            self.clear_buffer()
            self._zero = None
        self._width = width

        mode = self.config.linearize
        if mode != "off":
            r, g, b = linearise(r, mode), linearise(g, mode), linearise(b, mode)
            luma = linearise(luma, mode)

        self._buffer.append(np.stack([r, g, b], axis=0))
        average = np.mean(np.stack(list(self._buffer), axis=0), axis=0)
        r = average[0] * float(self.config.gain_r)
        g = average[1] * float(self.config.gain_g)
        b = average[2] * float(self.config.gain_b)
        intensity = (r + g + b) / 3.0
        raw = intensity.copy()
        if self._dark is not None:
            intensity = intensity - self._dark
        # Zero reference is captured after dark subtraction, preventing double subtraction.
        self._last_raw = intensity.copy()

        if self._zero is not None and self._zero.shape == intensity.shape:
            intensity = intensity - self._zero
        elif self._zero is not None:
            self._zero = None

        display = intensity
        if self.config.savgol_enabled and width >= 3:
            window = int(self.config.savgol_window) | 1
            order = int(self.config.savgol_order)
            if window > width:
                window = width if width % 2 == 1 else width - 1
            if window > order + 1:
                display = savitzky_golay(display, window, order)

        if self.config.hold_peaks:
            if self._hold is None or self._hold.shape != display.shape:
                self._hold = display.copy()
            else:
                self._hold = np.maximum(self._hold, display)
            display = self._hold
        else:
            self._hold = None

        pixel_values = np.asarray(crop, dtype=np.uint8)
        saturated_ratio = float(np.count_nonzero(pixel_values >= 250) / max(1, pixel_values.size))
        saturated = bool(np.max(pixel_values) >= 250 and saturated_ratio >= 0.002)

        return SpectrumResult(
            pixels=np.arange(int(pixel_start), int(pixel_start) + width, dtype=int),
            r=np.asarray(r, dtype=np.float32),
            g=np.asarray(g, dtype=np.float32),
            b=np.asarray(b, dtype=np.float32),
            luma=np.asarray(luma, dtype=np.float32),
            intensity=np.asarray(intensity, dtype=np.float32),
            display=np.asarray(display, dtype=np.float32),
            wavelengths=None,
            saturated=saturated,
            saturated_ratio=saturated_ratio,
            raw_max=float(np.max(pixel_values)) if pixel_values.size else 0.0,
            buffer_len=len(self._buffer),
            zero_active=self._zero is not None,
            hold_active=bool(self.config.hold_peaks),
            raw_intensity=raw,
            dark_profile=self._dark,
            dark_metadata=self._dark_metadata,
        )
