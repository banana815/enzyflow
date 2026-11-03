"""Camera/device discovery and frame sources.

The spectrometer used for the first adaptation is a UVC device:

    Sonix Technology Co., Ltd. USB 2.0 Camera (0c68:6464)
    /dev/video2 (capture), /dev/video3 (metadata)

It is *not* the integrated laptop camera.  Discovery therefore prefers an
external UVC capture node and can remember the USB VID:PID, so re-plugging
the spectrometer to a different /dev/videoN keeps working.

Three sources are provided:

* :class:`CameraSource`  - live UVC/V4L2 camera
* :class:`VideoFileSource` - recorded video or still image (offline work)
* :class:`SyntheticSource` - deterministic spectrum for demos and tests
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .color import wavelength_to_rgb
from .config import CameraConfig

__all__ = [
    "DeviceInfo",
    "CameraSource",
    "VideoFileSource",
    "SyntheticSource",
    "enumerate_devices",
    "select_device",
    "open_source",
]

_V4L2 = shutil.which("v4l2-ctl")


def _run(cmd: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _sysfs_capture(node: str) -> bool:
    """Best-effort capture-capability check if v4l2-ctl is unavailable."""
    cap = cv2.VideoCapture(node, cv2.CAP_V4L2)
    try:
        if not cap.isOpened():
            return False
        # Metadata nodes usually report no frame dimensions.
        return float(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) > 0
    finally:
        cap.release()


@dataclass
class DeviceInfo:
    path: str
    name: str
    vid_pid: str
    driver: str = ""
    capture: bool = True
    by_id: str = ""
    index: int = 0

    @property
    def label(self) -> str:
        return f"{self.path}  {self.name}  [{self.vid_pid}]"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "vid_pid": self.vid_pid,
            "driver": self.driver,
            "capture": self.capture,
            "by_id": self.by_id,
            "index": self.index,
        }


def enumerate_devices() -> list[DeviceInfo]:
    """Enumerate V4L2 video nodes and mark the capture-capable ones."""
    by_id: dict[str, str] = {}
    id_dir = Path("/dev/v4l/by-id")
    if id_dir.is_dir():
        for link in id_dir.iterdir():
            try:
                target = f"/dev/{link.resolve().name}"
            except OSError:
                continue
            by_id.setdefault(target, link.name)

    devices: list[DeviceInfo] = []
    for sys_node in sorted(
        Path("/sys/class/video4linux").glob("video*"),
        key=lambda p: int(re.sub(r"\D", "", p.name) or 0),
    ):
        node = f"/dev/{sys_node.name}"
        if not Path(node).exists():
            continue
        name = _read_text(sys_node / "name") or sys_node.name
        uevent = _read_text(sys_node / "device" / "uevent")
        vid_pid = ""
        driver = ""
        for line in uevent.splitlines():
            if line.startswith("PRODUCT="):
                parts = line.split("=", 1)[1].split("/")
                if len(parts) >= 2:
                    try:
                        vid_pid = f"{int(parts[0], 16):04x}:{int(parts[1], 16):04x}"
                    except ValueError:
                        vid_pid = "/".join(parts[:2])
            elif line.startswith("DRIVER="):
                driver = line.split("=", 1)[1].strip()

        capture = True
        if _V4L2:
            result = _run([_V4L2, "-d", node, "--info"])
            if result.returncode == 0:
                caps = ""
                in_device_caps = False
                for line in result.stdout.splitlines():
                    if "Device Caps" in line:
                        in_device_caps = True
                        caps = line
                    elif in_device_caps:
                        if line.strip() and not line.startswith(("\t", " ")):
                            break
                        caps += "\n" + line
                capture = "Video Capture" in caps
            else:
                capture = False
        else:
            capture = _sysfs_capture(node)

        devices.append(
            DeviceInfo(
                path=node,
                name=name,
                vid_pid=vid_pid,
                driver=driver,
                capture=capture,
                by_id=by_id.get(node, ""),
                index=int(re.sub(r"\D", "", sys_node.name) or 0),
            )
        )
    return devices


def _matches(device: DeviceInfo, text: str) -> bool:
    haystack = f"{device.name} {device.by_id} {device.vid_pid}".lower()
    return text.lower() in haystack


def select_device(cfg: CameraConfig, explicit: str | None = None) -> DeviceInfo:
    """Resolve a capture device using CLI, saved config, VID:PID or name."""
    devices = enumerate_devices()
    capture_devices = [d for d in devices if d.capture]

    if explicit:
        wanted = explicit
        if wanted.isdigit():
            wanted = f"/dev/video{wanted}"
        for device in devices:
            if wanted in {device.path, device.by_id} or _matches(device, wanted):
                if not device.capture:
                    raise RuntimeError(f"{device.path} is not a video-capture node")
                return device
        if Path(wanted).exists():
            return DeviceInfo(path=wanted, name=wanted, vid_pid="", capture=True)
        raise RuntimeError(f"camera device not found: {explicit}")

    if cfg.device:
        for device in capture_devices:
            if (device.path == cfg.device or device.by_id == cfg.device) and (
                not cfg.vid_pid or device.vid_pid.lower() == cfg.vid_pid.lower()
            ):
                return device
        # Saved node disappeared; fall through to VID:PID/name discovery.

    if cfg.vid_pid:
        for device in capture_devices:
            if device.vid_pid.lower() == cfg.vid_pid.lower():
                return device

    for needle in cfg.match:
        for device in capture_devices:
            if _matches(device, needle):
                return device

    candidates = [
        device
        for device in capture_devices
        if not any(_matches(device, word) for word in cfg.exclude)
    ]
    if len(candidates) == 1:
        return candidates[0]

    listing = "\n".join(
        f"  {d.path}  {d.name}  [{d.vid_pid}]  {'capture' if d.capture else 'metadata'}"
        for d in devices
    ) or "  (no /dev/video* devices found)"
    raise RuntimeError(
        "could not choose a spectrometer camera automatically.\n"
        f"Available devices:\n{listing}\n"
        "Use --device /dev/videoN (or set camera.device in runtime/config.json)."
    )


class CameraSource:
    """Live V4L2 camera with format fallback and v4l2-ctl controls."""

    def __init__(
        self,
        device: str,
        *,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        fourcc: str = "MJPG",
    ) -> None:
        self.device = device
        self.cap: cv2.VideoCapture | None = None
        self.actual_width = width
        self.actual_height = height
        self.actual_fps = fps
        self.fourcc_used = ""
        self.paced = False

        attempts: list[str | None] = [fourcc.upper(), "YUYV", None]
        last_error = ""
        for attempt in attempts:
            cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap.release()
                last_error = "could not open device"
                continue
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if attempt:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*attempt))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
            cap.set(cv2.CAP_PROP_FPS, int(fps))

            ok = False
            for _ in range(8):
                ok, frame = cap.read()
                if ok and frame is not None and frame.size:
                    break
                time.sleep(0.05)
            if ok:
                self.cap = cap
                self.fourcc_used = attempt or "default"
                self.actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or width)
                self.actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or height)
                self.actual_fps = float(cap.get(cv2.CAP_PROP_FPS) or fps)
                break
            cap.release()
            last_error = f"no frames with {attempt or 'default format'}"

        if self.cap is None:
            raise RuntimeError(f"{device}: {last_error}")

        self._controls_cache: dict[str, dict] | None = None

    # -------------------------------------------------------------- metadata
    @property
    def info(self) -> dict:
        return {
            "kind": "uvc_camera",
            "path": self.device,
            "width": self.actual_width,
            "height": self.actual_height,
            "fps": round(float(self.actual_fps), 3),
            "fourcc": self.fourcc_used,
        }

    def read(self) -> np.ndarray | None:
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        return frame

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self) -> "CameraSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- controls
    def controls(self) -> dict[str, dict]:
        if self._controls_cache is not None:
            return self._controls_cache
        result: dict[str, dict] = {}
        if _V4L2:
            proc = _run([_V4L2, "-d", self.device, "--list-ctrls"])
            if proc.returncode == 0:
                pattern = re.compile(r"^\s*([a-z0-9_]+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)$")
                for line in proc.stdout.splitlines():
                    match = pattern.match(line)
                    if not match:
                        continue
                    name, kind, rest = match.groups()
                    values: dict[str, object] = {"type": kind}
                    for key in ("min", "max", "step", "default", "value"):
                        found = re.search(rf"\b{key}=(-?\d+)", rest)
                        if found:
                            values[key] = int(found.group(1))
                    flags = re.search(r"flags=([^)]+)", rest)
                    if flags:
                        values["flags"] = flags.group(1).strip()
                    menu = re.search(r"value=\d+\s*\((.*?)\)", rest)
                    if menu:
                        values["menu"] = menu.group(1)
                    result[name] = values
        if not result:
            # Fallback for systems without v4l-utils.
            if self.cap is not None:
                for name, prop in (
                    ("brightness", cv2.CAP_PROP_BRIGHTNESS),
                    ("contrast", cv2.CAP_PROP_CONTRAST),
                    ("saturation", cv2.CAP_PROP_SATURATION),
                    ("gain", cv2.CAP_PROP_GAIN),
                    ("exposure_time_absolute", cv2.CAP_PROP_EXPOSURE),
                ):
                    result[name] = {"type": "int", "value": int(self.cap.get(prop))}
        self._controls_cache = result
        return result

    def set_control(self, name: str, value: int) -> None:
        if _V4L2:
            proc = _run([_V4L2, "-d", self.device, "--set-ctrl", f"{name}={int(value)}"])
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or f"failed to set {name}").strip())
            self._controls_cache = None
            return
        mapping = {
            "brightness": cv2.CAP_PROP_BRIGHTNESS,
            "contrast": cv2.CAP_PROP_CONTRAST,
            "saturation": cv2.CAP_PROP_SATURATION,
            "gain": cv2.CAP_PROP_GAIN,
            "exposure_time_absolute": cv2.CAP_PROP_EXPOSURE,
        }
        if name not in mapping or self.cap is None:
            raise RuntimeError(f"control not supported without v4l2-ctl: {name}")
        self.cap.set(mapping[name], value)

    def set_exposure_auto(self, auto: bool) -> None:
        if _V4L2:
            # V4L2: 1 = manual, 3 = aperture-priority/auto.
            self.set_control("auto_exposure", 3 if auto else 1)
        elif self.cap is not None:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3 if auto else 1)

    def set_exposure(self, value: int) -> None:
        value = max(1, min(5000, int(value)))
        self.set_exposure_auto(False)
        self.set_control("exposure_time_absolute", value)

    def get_exposure(self) -> int | None:
        controls = self.controls()
        item = controls.get("exposure_time_absolute")
        if item and isinstance(item.get("value"), int):
            return int(item["value"])
        return None

    def apply_optional_controls(self, cfg: CameraConfig) -> None:
        if cfg.white_balance_automatic is not None:
            try:
                self.set_control("white_balance_automatic", 1 if cfg.white_balance_automatic else 0)
            except RuntimeError:
                pass
        if (
            cfg.white_balance_temperature is not None
            and not cfg.white_balance_automatic
        ):
            try:
                self.set_control("white_balance_temperature", int(cfg.white_balance_temperature))
            except RuntimeError:
                pass
        for name in ("brightness", "contrast", "saturation", "gamma", "sharpness"):
            value = getattr(cfg, name, None)
            if value is not None:
                try:
                    self.set_control(name, int(value))
                except RuntimeError:
                    pass


class VideoFileSource:
    """Recorded video or still image, useful for offline analysis."""

    _IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

    def __init__(self, path: str, *, loop: bool = True) -> None:
        self.path = str(path)
        self.paced = True
        self.loop = loop
        self._image: np.ndarray | None = None
        self.cap: cv2.VideoCapture | None = None
        if Path(self.path).suffix.lower() in self._IMAGE_SUFFIXES:
            image = cv2.imread(self.path, cv2.IMREAD_COLOR)
            if image is None:
                raise RuntimeError(f"could not read image: {self.path}")
            self._image = image
            self.actual_width = image.shape[1]
            self.actual_height = image.shape[0]
            self.actual_fps = 30.0
        else:
            self.cap = cv2.VideoCapture(self.path)
            if not self.cap.isOpened():
                raise RuntimeError(f"could not open video: {self.path}")
            self.actual_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            self.actual_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            self.actual_fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 30.0)
        self.fourcc_used = "file"

    @property
    def info(self) -> dict:
        return {
            "kind": "video_file",
            "path": self.path,
            "width": self.actual_width,
            "height": self.actual_height,
            "fps": round(float(self.actual_fps), 3),
            "fourcc": self.fourcc_used,
        }

    def read(self) -> np.ndarray | None:
        if self._image is not None:
            return self._image.copy()
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        if not ok and self.loop:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        return frame if ok else None

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self) -> "VideoFileSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SyntheticSource:
    """Deterministic, physically-plausible spectrum image.

    Useful when no spectrometer is connected, for GUI demos, and as a
    hardware-free target for the tests.  The pixel->wavelength mapping is a
    gentle quadratic, so it exercises the polynomial calibration code.
    """

    EMISSION_LINES: tuple[tuple[float, float, float], ...] = (
        # wavelength_nm, amplitude, sigma_nm
        (405.4, 28.0, 1.8),
        (436.6, 90.0, 2.0),
        (487.7, 38.0, 2.0),
        (542.4, 72.0, 2.0),
        (546.5, 190.0, 1.9),
        (577.0, 24.0, 2.0),
        (611.6, 105.0, 2.2),
        (631.1, 34.0, 2.2),
        (700.0, 52.0, 2.6),
    )

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        *,
        noise: float = 1.2,
        seed: int = 7,
        band_height: int | None = None,
    ) -> None:
        self.actual_width = int(width)
        self.actual_height = int(height)
        self.actual_fps = 30.0
        self.fourcc_used = "synthetic"
        self.paced = True
        self.noise = float(noise)
        self.rng = np.random.default_rng(seed)
        self.phase = 0.0
        self.band_height = int(band_height or max(5, min(11, height // 90)))

    @staticmethod
    def wavelength_at(x: np.ndarray | float, width: int = 1280) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        x_equiv = x * (1279.0 / max(1, width - 1))
        return 380.0 + 0.28 * x_equiv + 1.2e-5 * x_equiv**2

    @property
    def info(self) -> dict:
        return {
            "kind": "synthetic",
            "path": "synthetic",
            "width": self.actual_width,
            "height": self.actual_height,
            "fps": self.actual_fps,
            "fourcc": self.fourcc_used,
        }

    def _render(self) -> np.ndarray:
        w, h = self.actual_width, self.actual_height
        x = np.arange(w, dtype=float)
        wavelength = self.wavelength_at(x, w)
        intensity = 16.0 + 12.0 * np.exp(-0.5 * ((wavelength - 550.0) / 190.0) ** 2)
        for center, amplitude, sigma in self.EMISSION_LINES:
            intensity += amplitude * np.exp(-0.5 * ((wavelength - center) / sigma) ** 2)
        intensity *= 1.0 + 0.002 * np.sin(self.phase)
        intensity = np.clip(intensity, 0.0, 255.0)

        rgb = np.asarray([wavelength_to_rgb(v) for v in wavelength], dtype=float) / 255.0
        # Keep a little white in every channel so the band is visible even for
        # deep red/near-IR pixels.
        tint = 0.45 + 0.55 * rgb
        frame = np.zeros((h, w, 3), dtype=np.float32)
        frame[:, :, 0] = 3.0 + self.rng.normal(0.0, self.noise * 0.5, (h, w))
        frame[:, :, 1] = 3.0 + self.rng.normal(0.0, self.noise * 0.5, (h, w))
        frame[:, :, 2] = 3.0 + self.rng.normal(0.0, self.noise * 0.5, (h, w))

        y0 = max(0, (h - self.band_height) // 2)
        y1 = min(h, y0 + self.band_height)
        rows = np.arange(y0, y1, dtype=float)
        center = (h - 1) / 2.0
        profile = np.exp(-0.5 * ((rows - center) / max(1.0, self.band_height / 3.0)) ** 2)
        for c in range(3):  # BGR
            frame[y0:y1, :, c] += (intensity * tint[:, 2 - c])[None, :] * (
                0.35 + 0.65 * profile[:, None]
            )
        frame += self.rng.normal(0.0, self.noise, frame.shape)
        self.phase += 0.04
        return np.clip(frame, 0, 255).astype(np.uint8)

    def read(self) -> np.ndarray:
        return self._render()

    def close(self) -> None:
        return None

    def __enter__(self) -> "SyntheticSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_source(
    cfg: CameraConfig,
    *,
    explicit_device: str | None = None,
    synthetic: bool = False,
    video: str | None = None,
) -> CameraSource | VideoFileSource | SyntheticSource:
    if synthetic and video:
        raise ValueError("--synthetic and --video are mutually exclusive")
    if synthetic:
        return SyntheticSource(cfg.width, cfg.height)
    if video:
        return VideoFileSource(video)
    device = select_device(cfg, explicit_device)
    source = CameraSource(
        device.path,
        width=cfg.width,
        height=cfg.height,
        fps=cfg.fps,
        fourcc=cfg.fourcc,
    )
    cfg.device = device.path
    if device.vid_pid:
        cfg.vid_pid = device.vid_pid
    source.apply_optional_controls(cfg)
    return source
