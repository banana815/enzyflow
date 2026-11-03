"""OpenCV GUI for the PC spectrometer.

The interaction model follows PySpectrometer2 (keyboard-driven, mouse
crosshairs, coloured graph) while the processing/ROI/zero-reference ideas
come from spectro-cam-rs.  The goal is not to clone either program, but to
provide a small, hackable application for the enzyflow experiment.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .calibration import Calibration, CalibrationError, generate_graticule
from .camera import CameraSource, SyntheticSource, VideoFileSource
from .color import wavelength_to_rgb
from .config import AppConfig, save_config
from .peaks import find_peaks
from .recording import save_capture
from .roi import Roi, auto_roi, normalise_roi
from .spectrum import SpectrumProcessor, SpectrumResult

__all__ = ["SpectrometerApp"]

# BGR colours for the dark UI.
_BG = (16, 16, 20)
_PANEL = (27, 27, 33)
_GRID = (52, 52, 60)
_GRID_STRONG = (86, 86, 96)
_TEXT = (225, 225, 225)
_DIM = (145, 145, 155)
_ACCENT = (120, 220, 255)
_GREEN = (110, 230, 140)
_YELLOW = (70, 220, 255)
_RED = (90, 90, 255)


def _text(
    image: np.ndarray,
    value: str,
    org: tuple[int, int],
    *,
    scale: float = 0.48,
    color: tuple[int, int, int] = _TEXT,
    thickness: int = 1,
) -> None:
    cv2.putText(
        image,
        str(value),
        (int(org[0]), int(org[1])),
        cv2.FONT_HERSHEY_SIMPLEX,
        float(scale),
        color,
        int(thickness),
        cv2.LINE_AA,
    )


def _fmt(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


class SpectrometerApp:
    WINDOW = "Enzyflow Spectrometer"
    WATERFALL_WINDOW = "Enzyflow Spectrometer - Waterfall"

    def __init__(
        self,
        config: AppConfig,
        config_path: Path,
        data_dir: Path,
        source: CameraSource | VideoFileSource | SyntheticSource,
        calibration: Calibration | None = None,
        *,
        waterfall: bool = False,
        fullscreen: bool = False,
        render_once: Path | None = None,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.data_dir = Path(data_dir)
        self.source = source
        self.calibration = calibration
        self.fullscreen = fullscreen
        self.render_once = Path(render_once) if render_once else None
        self.waterfall_enabled = bool(waterfall or config.display.waterfall)
        self.processor = SpectrumProcessor(config.processing)

        self.display_w = int(config.display.width)
        self.preview_h = int(config.display.preview_height)
        self.graph_h = int(config.display.graph_height)
        self.total_h = self.preview_h + self.graph_h
        self.preview_rect = (18, 18, 480, 270)
        self.status_x = 520
        self.strip_y = 124
        self.strip_h = 72
        self.strip_w = self.display_w - self.status_x - 20
        self.graph_top = self.preview_h
        self.graph_left = 58
        self.graph_right = 20
        self.graph_plot_top = self.preview_h + 30
        self.graph_bottom = 38
        self.plot_w = self.display_w - self.graph_left - self.graph_right
        self.plot_h = self.graph_h - 30 - self.graph_bottom

        self.frame: np.ndarray | None = None
        self.frame_w = int(getattr(source, "actual_width", config.camera.width) or config.camera.width)
        self.frame_h = int(getattr(source, "actual_height", config.camera.height) or config.camera.height)
        self.roi: Roi | None = None
        if config.roi.w > 0 and config.roi.h > 0 and not config.roi.auto:
            self.roi = normalise_roi(Roi(config.roi.x, config.roi.y, config.roi.w, config.roi.h), self.frame_w, self.frame_h)

        self.result: SpectrumResult | None = None
        self.last_canvas: np.ndarray | None = None
        self.last_graph: np.ndarray | None = None
        self.cal_points: list[int] = []
        self.measure = False
        self.rec_pixels = False
        self.mouse = (0, 0)
        self.drag_start: tuple[int, int] | None = None
        self.drag_now: tuple[int, int] | None = None
        self.status = "ready"
        self.last_save = "no capture yet"
        self.seq = 0
        self.failures = 0
        self.running = True
        self.fps_values: deque[float] = deque(maxlen=30)
        self.fps = 0.0
        self.frame_times: deque[float] = deque(maxlen=30)
        self.waterfall = np.zeros((320, self.display_w, 3), dtype=np.uint8)
        self._waterfall_window_open = False
        self._last_frame_time = time.monotonic()

        self.capture_dir = Path(config.export.directory)
        if not self.capture_dir.is_absolute():
            self.capture_dir = self.data_dir / self.capture_dir
        self.exposure_auto = bool(config.camera.exposure_auto)
        self.exposure_value: int | None = config.camera.exposure
        self._configure_camera()

    # -------------------------------------------------------------- camera setup
    def _configure_camera(self) -> None:
        if not isinstance(self.source, CameraSource):
            return
        try:
            if self.exposure_auto:
                self.source.set_exposure_auto(True)
            elif self.exposure_value is not None:
                self.source.set_exposure(self.exposure_value)
            else:
                self.source.set_exposure_auto(True)
                self.exposure_auto = True
            current = self.source.get_exposure()
            if current is not None:
                self.exposure_value = current
        except Exception as exc:  # v4l2 controls are optional
            print(f"[camera] exposure setup failed: {exc}")

    def _toggle_exposure(self) -> None:
        if not isinstance(self.source, CameraSource):
            self.status = "synthetic/video source has no exposure control"
            print(self.status)
            return
        self.exposure_auto = not self.exposure_auto
        try:
            if self.exposure_auto:
                self.source.set_exposure_auto(True)
                self.status = "exposure: AUTO"
            else:
                value = self.exposure_value or self.source.get_exposure() or 300
                self.source.set_exposure(int(value))
                self.exposure_value = int(value)
                self.status = f"exposure: MANUAL {value}"
        except Exception as exc:
            self.status = f"exposure change failed: {exc}"
        self.config.camera.exposure_auto = self.exposure_auto
        self.config.camera.exposure = self.exposure_value
        print(f"[camera] {self.status}")

    def _adjust_exposure(self, direction: int) -> None:
        if not isinstance(self.source, CameraSource):
            return
        if self.exposure_auto:
            self.exposure_auto = False
            self.source.set_exposure_auto(False)
        base = self.exposure_value or self.source.get_exposure() or 300
        # A multiplicative step is much more useful for UVC cameras: the
        # useful range here is 1..5000 (100 us units) and linear +/-50 would
        # need dozens of key presses to go from 157 to 5000.
        factor = 1.25 if direction > 0 else 0.8
        value = max(1, min(5000, int(round(base * factor))))
        try:
            self.source.set_exposure(value)
            self.exposure_value = value
            self.status = f"exposure: MANUAL {value}"
            self.config.camera.exposure_auto = False
            self.config.camera.exposure = value
        except Exception as exc:
            self.status = f"exposure change failed: {exc}"
        print(f"[camera] {self.status}")

    # ------------------------------------------------------------------ ROI
    def _ensure_roi(self) -> None:
        if self.frame is None:
            return
        if (
            self.roi is None
            or self.config.roi.auto
            or self.roi.w <= 0
            or self.roi.h <= 0
            or self.roi.x + self.roi.w > self.frame_w
            or self.roi.y + self.roi.h > self.frame_h
        ):
            self.roi = auto_roi(self.frame, self.config.roi.auto_height)
            self.config.roi.auto = False
            self._remember_roi()
            print(f"[roi] auto: x={self.roi.x} y={self.roi.y} w={self.roi.w} h={self.roi.h}")

    def _remember_roi(self) -> None:
        if self.roi is None:
            return
        self.config.roi.x = int(self.roi.x)
        self.config.roi.y = int(self.roi.y)
        self.config.roi.w = int(self.roi.w)
        self.config.roi.h = int(self.roi.h)

    def _set_roi(self, roi: Roi) -> None:
        self.roi = normalise_roi(roi, self.frame_w, self.frame_h)
        self.config.roi.auto = False
        self._remember_roi()
        self.processor.clear_buffer()
        print(f"[roi] x={self.roi.x} y={self.roi.y} w={self.roi.w} h={self.roi.h}")

    def _auto_roi_now(self) -> None:
        if self.frame is None:
            return
        self._set_roi(auto_roi(self.frame, self.config.roi.auto_height))

    def _reset_roi(self) -> None:
        height = max(1, min(200, self.config.roi.auto_height))
        y = max(0, (self.frame_h - height) // 2)
        self._set_roi(Roi(0, y, self.frame_w, height))

    # ------------------------------------------------------------- calibration
    def _calibrate(self) -> None:
        points = sorted(set(int(p) for p in self.cal_points))
        if len(points) < 3:
            print("[cal] mark at least 3 peaks with 'p' first")
            self.status = "calibration needs >= 3 marked peaks"
            return
        if not sys.stdin.isatty():
            print("[cal] no terminal for interactive calibration; use the 'calibrate' command")
            self.status = "no interactive terminal for calibration"
            return

        print("\n=== calibration ===")
        calibration_input: list[tuple[int, float]] = []
        for pixel in points:
            guess = self.calibration.wavelength_at(pixel) if self.calibration else None
            hint = f" (current guess {guess:.1f} nm)" if guess is not None else ""
            while True:
                raw = input(f"pixel {pixel}px -> known wavelength in nm{hint}: ").strip()
                if not raw:
                    print("[cal] aborted")
                    return
                try:
                    wavelength = float(raw.replace(",", "."))
                except ValueError:
                    print("  enter a number, for example 546.5")
                    continue
                if not (100.0 <= wavelength <= 3000.0):
                    print("  wavelength must be between 100 and 3000 nm")
                    continue
                break
            calibration_input.append((pixel, wavelength))

        try:
            calibration = Calibration.fit(
                calibration_input,
                note="GUI calibration",
                frame_width=self.frame_w,
            )
        except CalibrationError as exc:
            print(f"[cal] failed: {exc}")
            self.status = f"calibration failed: {exc}"
            return

        path = self.data_dir / "calibration.json"
        calibration.save(path)
        self.calibration = calibration
        self.cal_points.clear()
        self.processor.clear_buffer()
        print(f"[cal] saved {path}")
        print(f"[cal] {calibration.describe()}")
        print(f"[cal] coefficients (highest order first): {calibration.coefficients}")
        self.status = f"calibrated: {calibration.describe()}"

    def _clear_cal_points(self) -> None:
        self.cal_points.clear()
        self.status = "calibration points cleared"

    # ------------------------------------------------------------------ saving
    def _exposure_meta(self) -> dict:
        return {
            "auto": bool(self.exposure_auto),
            "value": int(self.exposure_value) if self.exposure_value is not None else None,
            "units": "V4L2 exposure_time_absolute (100 us units on this camera)",
        }

    def _save(self) -> None:
        if self.result is None or self.frame is None:
            self.status = "nothing to save yet"
            return
        if self.result.wavelengths is None and self.calibration is not None and self.roi is not None:
            self.result.wavelengths = self.calibration.wavelengths(self.roi.x, self.roi.w)
        if self.last_graph is None:
            self.last_graph = self._render_graph()
        try:
            files = save_capture(
                self.capture_dir,
                self.result,
                calibration=self.calibration,
                roi=self.roi or Roi(0, 0, self.frame_w, self.frame_h),
                source_info=self.source.info,
                exposure=self._exposure_meta(),
                seq=self.seq,
                graph_image=self.last_graph,
                ui_image=self.last_canvas,
                frame=self.frame,
                save_frame=self.config.export.save_frame,
                save_ui=self.config.export.save_ui,
            )
        except Exception as exc:
            print(f"[save] failed: {exc}")
            self.status = f"save failed: {exc}"
            return
        self.last_save = time.strftime("%H:%M:%S")
        self.status = f"saved {Path(files['csv']).name}"
        print(f"[save] {files['csv']}")
        print(f"[save] {files['json']}")

    # ---------------------------------------------------------------- rendering
    def _render_preview(self, canvas: np.ndarray) -> None:
        x0, y0, w, h = self.preview_rect
        cv2.rectangle(canvas, (x0 - 1, y0 - 1), (x0 + w, y0 + h), _GRID_STRONG, 1)
        if self.frame is None:
            cv2.rectangle(canvas, (x0, y0), (x0 + w, y0 + h), _PANEL, -1)
            _text(canvas, "waiting for frames...", (x0 + 12, y0 + h // 2), color=_DIM)
            return
        small = cv2.resize(self.frame, (w, h), interpolation=cv2.INTER_AREA)
        canvas[y0 : y0 + h, x0 : x0 + w] = small
        if self.roi is not None:
            sx = w / max(1, self.frame_w)
            sy = h / max(1, self.frame_h)
            p1 = (x0 + int(self.roi.x * sx), y0 + int(self.roi.y * sy))
            p2 = (
                x0 + int((self.roi.x + self.roi.w) * sx),
                y0 + int((self.roi.y + self.roi.h) * sy),
            )
            cv2.rectangle(canvas, p1, p2, _GREEN, 1)
            cv2.putText(
                canvas,
                "ROI",
                (p1[0] + 3, max(y0 + 12, p1[1] - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                _GREEN,
                1,
                cv2.LINE_AA,
            )
        if self.drag_start is not None and self.drag_now is not None:
            p1 = (x0 + self.drag_start[0], y0 + self.drag_start[1])
            p2 = (x0 + self.drag_now[0], y0 + self.drag_now[1])
            cv2.rectangle(canvas, p1, p2, _YELLOW, 1)
        _text(canvas, "LIVE PREVIEW  (drag a band, or click to centre it)", (x0 + 4, y0 + 14), scale=0.4, color=_DIM)

    def _render_strip(self, canvas: np.ndarray) -> None:
        if self.frame is None or self.roi is None:
            return
        crop = self.frame[self.roi.y : self.roi.y + self.roi.h, self.roi.x : self.roi.x + self.roi.w]
        if crop.size == 0:
            return
        interpolation = cv2.INTER_NEAREST if crop.shape[0] <= 8 else cv2.INTER_AREA
        strip = cv2.resize(crop, (self.strip_w, self.strip_h), interpolation=interpolation)
        x0 = self.status_x
        y0 = self.strip_y
        canvas[y0 : y0 + self.strip_h, x0 : x0 + self.strip_w] = strip
        cv2.rectangle(canvas, (x0 - 1, y0 - 1), (x0 + self.strip_w, y0 + self.strip_h), _GRID_STRONG, 1)
        _text(canvas, "ROI STRIP  (colour tells you where the spectrum lands)", (x0, y0 - 7), scale=0.4, color=_DIM)
        # Original-pixel ticks every 100 px.
        if self.roi.w > 0:
            for offset in range(0, self.roi.w, 100):
                gx = x0 + int(offset * self.strip_w / self.roi.w)
                cv2.line(canvas, (gx, y0), (gx, y0 + 5), _DIM, 1)
                _text(canvas, str(self.roi.x + offset), (gx + 2, y0 + self.strip_h - 5), scale=0.3, color=_DIM)

    def _render_status(self, canvas: np.ndarray) -> None:
        x = self.status_x
        info = self.source.info
        cal_status = self.calibration.describe() if self.calibration else "UNCALIBRATED"
        low_light = False
        if self.result is not None:
            low_light = self.result.raw_max < 5.0
            signal = (
                "LOW LIGHT - open the light path or raise exposure"
                if low_light
                else f"max {self.result.raw_max:.0f} | buffer {self.result.buffer_len}"
            )
            sat = "SATURATED" if self.result.saturated else "ok"
        else:
            signal, sat = "waiting", "-"
        exposure = "AUTO" if self.exposure_auto else f"MAN {self.exposure_value or '-'}"
        lines = [
            ("ENZYFLOW / USB SPECTROMETER", _ACCENT, 0.58, 30),
            (f"source  {info.get('path', '?')}   {info.get('width', '?')}x{info.get('height', '?')} @ {info.get('fps', '?')} fps  {info.get('fourcc', '')}", _TEXT, 0.42, 52),
            (f"ROI     x={self.roi.x if self.roi else '-'} y={self.roi.y if self.roi else '-'} w={self.roi.w if self.roi else '-'} h={self.roi.h if self.roi else '-'}", _TEXT, 0.42, 74),
            (f"camera  exposure {exposure}   buffer {self.config.processing.buffer_size}   fps {self.fps:.1f}", _TEXT, 0.42, 96),
            (f"cal     {cal_status}", _GREEN if self.calibration else _YELLOW, 0.4, 212),
            (f"signal  {signal}", _RED if low_light else _TEXT, 0.4, 232),
            (f"        saturation {sat}   zero {('on' if self.result and self.result.zero_active else 'off')}", _TEXT, 0.4, 248),
            (f"last    {self.last_save}   {self.status}", _DIM, 0.38, 264),
        ]
        for value, color, scale, y in lines:
            _text(canvas, value, (x, y), scale=scale, color=color)

        help_lines = [
            "s save   h hold   p mark peak   c calibrate   x clear marks   m measure   a auto ROI   r reset ROI",
            "e exposure   [ ] adjust   z zero   Z clear zero   f filter   o/l window   i/k peak distance   u/j threshold   w waterfall   q quit",
        ]
        for index, line in enumerate(help_lines):
            _text(canvas, line, (18, self.preview_h - 18 + index * 15), scale=0.34, color=_DIM)

    def _pixel_to_graph_x(self, pixel: float) -> int:
        if self.roi is None:
            return self.graph_left
        n = max(1, self.roi.w - 1)
        return int(self.graph_left + (float(pixel) - self.roi.x) * self.plot_w / n)

    def _graph_index_from_x(self, x: int) -> int | None:
        if self.roi is None or self.result is None or self.result.display.size == 0:
            return None
        if not (self.graph_left <= x <= self.graph_left + self.plot_w):
            return None
        fraction = (x - self.graph_left) / max(1, self.plot_w)
        return int(round(fraction * (self.result.display.size - 1)))

    def _render_graph(self) -> np.ndarray:
        # Local graph coordinates: this image is pasted into the UI canvas at
        # self.graph_top, so do not use absolute canvas offsets here.
        graph = np.full((self.graph_h, self.display_w, 3), (12, 12, 16), dtype=np.uint8)
        x0 = self.graph_left
        y0 = 30
        y1 = y0 + self.plot_h
        x1 = x0 + self.plot_w
        result = self.result
        if result is None or result.display.size == 0:
            _text(graph, "no spectrum yet", (x0 + 12, y0 + self.plot_h // 2), scale=0.7, color=_DIM)
            return graph

        display = np.asarray(result.display, dtype=float)
        values = np.clip(display, 0.0, None)
        vmax = float(np.max(values)) if values.size else 0.0
        if vmax <= 1e-9:
            vmax = 1.0
        quiet = vmax < 1.5 and result.raw_max < 10.0
        n = values.size
        wl = result.wavelengths

        # Horizontal grid and y labels.
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            gy = int(y1 - frac * self.plot_h)
            cv2.line(graph, (x0, gy), (x1, gy), _GRID if frac not in (0.0, 1.0) else _GRID_STRONG, 1)
            label = f"{vmax * frac:.2f}" if vmax < 10 else f"{vmax * frac:.0f}"
            _text(graph, label, (6, gy + 4), scale=0.35, color=_DIM)

        # Vertical graticule.
        if self.calibration is not None and self.roi is not None:
            tens, fifties = generate_graticule(self.calibration, self.roi.x, self.roi.w)
            for index, _ in tens:
                gx = x0 + int(index * self.plot_w / max(1, n - 1))
                cv2.line(graph, (gx, y0), (gx, y1), _GRID, 1)
            for index, wavelength in fifties:
                gx = x0 + int(index * self.plot_w / max(1, n - 1))
                cv2.line(graph, (gx, y0), (gx, y1), _GRID_STRONG, 1)
                _text(graph, f"{wavelength:.0f}nm", (gx - 18, y1 + 18), scale=0.36, color=_TEXT)
            _text(graph, "wavelength (nm)", (x1 - 110, y1 + 34), scale=0.38, color=_DIM)
        else:
            step = 100
            for pixel in range(0, self.frame_w, step):
                if self.roi is not None and not (self.roi.x <= pixel < self.roi.x + self.roi.w):
                    continue
                gx = self._pixel_to_graph_x(pixel)
                if x0 <= gx <= x1:
                    cv2.line(graph, (gx, y0), (gx, y1), _GRID, 1)
                    _text(graph, f"px{pixel}", (gx - 12, y1 + 18), scale=0.34, color=_DIM)
            _text(graph, "pixel (uncalibrated)", (x1 - 130, y1 + 34), scale=0.38, color=_DIM)

        # Spectrum as wavelength-coloured columns (or neutral when uncalibrated).
        for gx in range(self.plot_w):
            index = int(round(gx * (n - 1) / max(1, self.plot_w - 1)))
            value = values[index]
            gy = int(y1 - (value / vmax) * self.plot_h)
            if quiet:
                color = (105, 105, 105)
            elif wl is not None and wl.size > index:
                r, g, b = wavelength_to_rgb(float(wl[index]))
                color = (b, g, r)
            else:
                color = (200, 200, 200)
            cv2.line(graph, (x0 + gx, y1), (x0 + gx, gy), color, 1)

        # Peaks.
        if n >= 3 and not quiet:
            peaks = find_peaks(
                values,
                threshold=float(self.config.processing.peak_threshold),
                min_distance=int(self.config.processing.peak_min_distance),
            )
            if peaks.size:
                strongest = sorted(peaks.tolist(), key=lambda i: values[i], reverse=True)[:10]
                for index in sorted(strongest):
                    gx = x0 + int(index * self.plot_w / max(1, n - 1))
                    gy = int(y1 - (values[index] / vmax) * self.plot_h)
                    if wl is not None and wl.size > index:
                        label = f"{float(wl[index]):.1f}nm"
                    else:
                        label = f"px{int(result.pixels[index])}"
                    tx = min(max(x0 + 2, gx - 10), x1 - 75)
                    ty = max(y0 + 14, gy - 8)
                    cv2.rectangle(graph, (tx - 2, ty - 11), (tx + 68, ty + 2), _PANEL, -1)
                    cv2.rectangle(graph, (tx - 2, ty - 11), (tx + 68, ty + 2), _YELLOW, 1)
                    _text(graph, label, (tx + 2, ty - 1), scale=0.35, color=_YELLOW)
                    cv2.line(graph, (gx, ty + 2), (gx, gy), _YELLOW, 1)

        # Calibration markers.
        for pixel in self.cal_points:
            if self.roi is None or not (self.roi.x <= pixel < self.roi.x + self.roi.w):
                continue
            gx = self._pixel_to_graph_x(pixel)
            cv2.circle(graph, (gx, y0 + 12), 4, _YELLOW, -1)
            _text(graph, str(pixel), (gx - 10, y0 + 28), scale=0.32, color=_YELLOW)

        # Measurement cursor.
        if self.measure and x0 <= self.mouse[0] <= x1 and y0 <= self.mouse[1] <= y1:
            index = self._graph_index_from_x(self.mouse[0])
            if index is not None:
                gx = self.mouse[0]
                cv2.line(graph, (gx, y0), (gx, y1), _ACCENT, 1)
                if (
                    self.calibration is not None
                    and result.wavelengths is not None
                    and result.wavelengths.size > index
                ):
                    label = f"{float(result.wavelengths[index]):.2f} nm"
                else:
                    label = f"px {int(result.pixels[index])}"
                bx = min(max(x0, gx + 4), x1 - 110)
                cv2.rectangle(graph, (bx, y0 + 4), (bx + 108, y0 + 22), _PANEL, -1)
                _text(graph, label, (bx + 4, y0 + 18), scale=0.4, color=_ACCENT)

        # Titles and badges.
        _text(graph, "SPECTRUM", (x0, 20), scale=0.55, color=_ACCENT)
        cal_text = self.calibration.describe() if self.calibration else "UNCALIBRATED - press p on known peaks, then c"
        _text(graph, cal_text, (x0 + 92, 20), scale=0.4, color=_GREEN if self.calibration else _YELLOW)
        if result.hold_active:
            _text(graph, "HOLD", (x1 - 50, 20), scale=0.4, color=_YELLOW)
        if result.saturated:
            _text(graph, "SATURATED", (x1 - 120, 20), scale=0.4, color=_RED)
        if self.rec_pixels:
            _text(graph, "CLICK PEAKS", (x1 - 210, 20), scale=0.4, color=_YELLOW)
        if quiet:
            _text(graph, "LOW SIGNAL - open the slit, point at a lamp, or increase exposure", (x0 + 10, y0 + 24), scale=0.45, color=_RED)
            _text(graph, "no usable peak", (x0 + 4, y1 + 34), scale=0.36, color=_DIM)
        else:
            _text(graph, f"peak {result.peak_wavelength:.1f} nm" if result.peak_wavelength is not None else f"peak px {int(result.pixels[result.peak_index])}", (x0 + 4, y1 + 34), scale=0.36, color=_DIM)
        return graph

    def _render(self) -> np.ndarray:
        canvas = np.full((self.total_h, self.display_w, 3), _BG, dtype=np.uint8)
        self._render_preview(canvas)
        self._render_strip(canvas)
        self._render_status(canvas)
        graph = self._render_graph()
        canvas[self.graph_top : self.graph_top + self.graph_h, :, :] = graph
        cv2.line(canvas, (0, self.graph_top), (self.display_w, self.graph_top), _GRID_STRONG, 1)
        self.last_graph = graph
        self.last_canvas = canvas
        return canvas

    def _update_waterfall(self) -> None:
        if not self.waterfall_enabled or self.result is None:
            return
        result = self.result
        n = result.display.size
        if n == 0:
            return
        row = np.zeros((1, self.display_w, 3), dtype=np.uint8)
        for gx in range(self.display_w):
            index = int(round(gx * (n - 1) / max(1, self.display_w - 1)))
            if result.wavelengths is not None:
                r, g, b = wavelength_to_rgb(float(result.wavelengths[index]))
            else:
                r, g, b = 200, 200, 200
            luminosity = float(np.clip(result.display[index], 0.0, 255.0) / 255.0)
            row[0, gx] = (int(b * luminosity), int(g * luminosity), int(r * luminosity))
        self.waterfall = np.vstack((row, self.waterfall[:-1]))
        wf = self.waterfall.copy()
        # 50 nm lines, if calibrated.
        if self.calibration is not None and self.roi is not None:
            _, fifties = generate_graticule(self.calibration, self.roi.x, self.roi.w)
            for index, wavelength in fifties:
                gx = int(index * self.display_w / max(1, n - 1))
                for y in range(0, wf.shape[0], 12):
                    cv2.line(wf, (gx, y), (gx, min(wf.shape[0] - 1, y + 5)), (210, 210, 210), 1)
                _text(wf, f"{wavelength:.0f}", (max(0, gx - 10), wf.shape[0] - 6), scale=0.35, color=(230, 230, 230))
        _text(wf, "WATERFALL  (newest on top)", (10, 20), scale=0.45, color=(220, 220, 220))
        cv2.imshow(self.WATERFALL_WINDOW, wf)
        self._waterfall_window_open = True

    # ------------------------------------------------------------------ events
    def _mouse(self, event: int, x: int, y: int, flags: int, param: object) -> None:
        self.mouse = (int(x), int(y))
        px, py, pw, ph = self.preview_rect
        in_preview = px <= x <= px + pw and py <= y <= py + ph
        if event == cv2.EVENT_LBUTTONDOWN:
            if in_preview:
                self.drag_start = (int(x - px), int(y - py))
                self.drag_now = self.drag_start
            elif self.rec_pixels and self.graph_top <= y <= self.graph_top + self.graph_h:
                index = self._graph_index_from_x(x)
                if index is not None and self.result is not None:
                    pixel = int(self.result.pixels[index])
                    self.cal_points.append(pixel)
                    print(f"[cal] marked pixel {pixel}")
        elif event == cv2.EVENT_MOUSEMOVE and self.drag_start is not None:
            self.drag_now = (max(0, min(pw, int(x - px))), max(0, min(ph, int(y - py))))
        elif event == cv2.EVENT_LBUTTONUP and self.drag_start is not None:
            start = self.drag_start
            end = (max(0, min(pw, int(x - px))), max(0, min(ph, int(y - py))))
            self.drag_start = None
            self.drag_now = None
            x1, x2 = sorted((start[0], end[0]))
            y1, y2 = sorted((start[1], end[1]))
            # Convert preview coordinates to frame coordinates.
            fx1 = int(x1 * self.frame_w / max(1, pw))
            fx2 = int(x2 * self.frame_w / max(1, pw))
            fy1 = int(y1 * self.frame_h / max(1, ph))
            fy2 = int(y2 * self.frame_h / max(1, ph))
            if (fx2 - fx1) >= 8 and (fy2 - fy1) >= 1:
                self._set_roi(Roi(fx1, fy1, fx2 - fx1, fy2 - fy1))
            else:
                height = max(1, min(200, self.config.roi.auto_height))
                cy = int((start[1] + end[1]) / 2 * self.frame_h / max(1, ph))
                self._set_roi(Roi(0, max(0, cy - height // 2), self.frame_w, height))

    def _handle_key(self, key: int) -> None:
        if key in (255, -1):
            return
        char = chr(key) if 0 <= key < 256 else ""
        if char in {"q", "\x1b"}:
            self.running = False
        elif char == "s":
            self._save()
        elif char == "h":
            self.processor.set_hold(not self.config.processing.hold_peaks)
            self.status = f"hold peaks: {'on' if self.config.processing.hold_peaks else 'off'}"
        elif char == "m":
            self.rec_pixels = False
            self.measure = not self.measure
        elif char == "p":
            self.measure = False
            self.rec_pixels = not self.rec_pixels
            self.status = "click peaks on the graph" if self.rec_pixels else "peak marking off"
        elif char == "c":
            self._calibrate()
        elif char == "x":
            self._clear_cal_points()
        elif char == "a":
            self._auto_roi_now()
        elif char == "r":
            self._reset_roi()
        elif char == "e":
            self._toggle_exposure()
        elif char == "[":
            self._adjust_exposure(-1)
        elif char == "]":
            self._adjust_exposure(1)
        elif char == "b":
            self.processor.clear_buffer()
            self.status = "buffer cleared"
        elif char == "z":
            if self.processor.set_zero_reference():
                self.status = "zero reference set"
            else:
                self.status = "no spectrum yet"
        elif char == "Z":
            self.processor.clear_zero_reference()
            self.status = "zero reference cleared"
        elif char == "f":
            self.processor.set_savgol_enabled(not self.config.processing.savgol_enabled)
            self.status = f"smoothing: {'on' if self.config.processing.savgol_enabled else 'off'}"
        elif char == "o":
            self.processor.set_savgol_window(self.config.processing.savgol_window + 2)
        elif char == "l":
            self.processor.set_savgol_window(max(3, self.config.processing.savgol_window - 2))
        elif char == "i":
            self.config.processing.peak_min_distance = min(200, self.config.processing.peak_min_distance + 2)
        elif char == "k":
            self.config.processing.peak_min_distance = max(1, self.config.processing.peak_min_distance - 2)
        elif char == "u":
            self.config.processing.peak_threshold = min(0.95, self.config.processing.peak_threshold + 0.02)
        elif char == "j":
            self.config.processing.peak_threshold = max(0.01, self.config.processing.peak_threshold - 0.02)
        elif char == "w":
            self.waterfall_enabled = not self.waterfall_enabled
            self.config.display.waterfall = self.waterfall_enabled
            if not self.waterfall_enabled and self._waterfall_window_open:
                cv2.destroyWindow(self.WATERFALL_WINDOW)
                self._waterfall_window_open = False
        elif char == "?":
            print(__doc__ or "keys: s h p c x m a r e [ ] z Z f o l i k u j w q")

    # --------------------------------------------------------------------- run
    def _process_frame(self, frame: np.ndarray) -> SpectrumResult | None:
        self.frame = frame
        if frame.ndim != 3 or frame.shape[2] < 3:
            return None
        h, w = frame.shape[:2]
        if w != self.frame_w or h != self.frame_h:
            self.frame_w, self.frame_h = w, h
            self.roi = None
        self._ensure_roi()
        if self.roi is None:
            return None
        crop = self.roi.crop(frame)
        if crop.size == 0:
            return None
        result = self.processor.update(crop, self.roi.x)
        if self.calibration is not None:
            result.wavelengths = self.calibration.wavelengths(self.roi.x, self.roi.w)
        self.result = result
        self.seq += 1

        now = time.monotonic()
        self.frame_times.append(now)
        if len(self.frame_times) >= 2:
            span = self.frame_times[-1] - self.frame_times[0]
            if span > 0:
                self.fps = (len(self.frame_times) - 1) / span
        self._last_frame_time = now
        return result

    def _save_config(self) -> None:
        self._remember_roi()
        self.config.display.waterfall = self.waterfall_enabled
        self.config.camera.exposure_auto = self.exposure_auto
        self.config.camera.exposure = self.exposure_value
        try:
            save_config(self.config, self.config_path)
        except OSError as exc:
            print(f"[config] could not save: {exc}")

    def run(self) -> int:
        if self.render_once is not None:
            for _ in range(12):
                frame = self.source.read()
                if frame is not None:
                    self._process_frame(frame)
            canvas = self._render()
            self.render_once.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(self.render_once), canvas)
            print(f"[render] wrote {self.render_once}")
            self._save_config()
            self.source.close()
            return 0

        cv2.namedWindow(self.WINDOW, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.WINDOW, self.display_w, self.total_h)
        cv2.moveWindow(self.WINDOW, 40, 40)
        if self.fullscreen:
            cv2.setWindowProperty(self.WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.setMouseCallback(self.WINDOW, self._mouse)

        print("[gui] keys: s save | h hold | p mark peak | c calibrate | x clear | m measure")
        print("[gui]       a auto ROI | r reset ROI | e exposure | [ ] adjust | z zero | Z clear")
        print("[gui]       f filter | o/l window | i/k peak distance | u/j threshold | w waterfall | q quit")
        try:
            while self.running:
                frame = self.source.read()
                if frame is None:
                    self.failures += 1
                    if self.failures == 1:
                        print("[camera] no frame; retrying...")
                    if self.failures > 60:
                        print("[camera] stopped returning frames")
                        break
                    time.sleep(0.05)
                    continue
                self.failures = 0
                self._process_frame(frame)
                canvas = self._render()
                cv2.imshow(self.WINDOW, canvas)
                if self.waterfall_enabled:
                    self._update_waterfall()
                key = cv2.waitKey(1) & 0xFF
                self._handle_key(key)
                # Give the backend a few frames to create/map the window before
                # treating a zero visibility value as "user closed it".
                if self.seq > 5:
                    try:
                        if cv2.getWindowProperty(self.WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                            break
                    except cv2.error:
                        break
                if getattr(self.source, "paced", False):
                    time.sleep(0.005)
        except KeyboardInterrupt:
            pass
        finally:
            self._save_config()
            self.source.close()
            cv2.destroyAllWindows()
        return 0


def run_app(
    config: AppConfig,
    config_path: Path,
    data_dir: Path,
    source: CameraSource | VideoFileSource | SyntheticSource,
    calibration: Calibration | None = None,
    *,
    waterfall: bool = False,
    fullscreen: bool = False,
    render_once: Path | None = None,
) -> int:
    app = SpectrometerApp(
        config,
        config_path,
        data_dir,
        source,
        calibration,
        waterfall=waterfall,
        fullscreen=fullscreen,
        render_once=render_once,
    )
    return app.run()
