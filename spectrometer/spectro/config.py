"""Persistent configuration for the PC spectrometer."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

__all__ = [
    "CameraConfig",
    "RoiConfig",
    "ProcessingConfig",
    "DisplayConfig",
    "ExportConfig",
    "AppConfig",
    "default_data_dir",
    "load_config",
    "save_config",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    return PROJECT_ROOT / "runtime"


@dataclass
class CameraConfig:
    # Leave device empty to auto-select.  Once a device is found its vid:pid
    # is stored as well, so a re-plug that changes /dev/videoN still works.
    device: str | None = None
    vid_pid: str | None = None
    match: list[str] = field(default_factory=lambda: ["usb-zh", "sonix", "spectro"])
    exclude: list[str] = field(default_factory=lambda: ["integrated", "bison", "facetime"])
    width: int = 1280
    height: int = 720
    fps: int = 30
    fourcc: str = "MJPG"
    exposure_auto: bool = True
    exposure: int | None = None
    brightness: int | None = None
    contrast: int | None = None
    saturation: int | None = None
    gamma: int | None = None
    sharpness: int | None = None
    white_balance_automatic: bool | None = None
    white_balance_temperature: int | None = None


@dataclass
class RoiConfig:
    # x/y/w/h are absolute frame pixels.  auto=True requests one automatic
    # detection on the next start (or when pressing 'a' in the GUI).
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    auto: bool = True
    auto_height: int = 8


@dataclass
class ProcessingConfig:
    buffer_size: int = 8
    linearize: str = "off"  # off | rec601 | rec709 | srgb
    gain_r: float = 1.0
    gain_g: float = 1.0
    gain_b: float = 1.0
    savgol_enabled: bool = True
    savgol_window: int = 9
    savgol_order: int = 3
    hold_peaks: bool = False
    peak_threshold: float = 0.12
    peak_min_distance: int = 12


@dataclass
class DisplayConfig:
    width: int = 1280
    preview_height: int = 300
    graph_height: int = 420
    waterfall: bool = False
    show_help: bool = True


@dataclass
class ExportConfig:
    directory: str = "captures"
    save_frame: bool = True
    save_ui: bool = True


@dataclass
class AppConfig:
    version: int = 1
    camera: CameraConfig = field(default_factory=CameraConfig)
    roi: RoiConfig = field(default_factory=RoiConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    export: ExportConfig = field(default_factory=ExportConfig)

    # ---------------------------------------------------------------- (de)serial
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        config = cls()
        for section in fields(cls):
            name = section.name
            if name == "version":
                config.version = int(data.get("version", config.version))
                continue
            nested = getattr(config, name)
            values = data.get(name)
            if not isinstance(values, dict):
                continue
            for item in fields(nested):
                if item.name in values:
                    setattr(nested, item.name, values[item.name])
        config.normalise()
        return config

    # ------------------------------------------------------------------ sanity
    def normalise(self) -> None:
        c = self.camera
        c.width = max(160, int(c.width))
        c.height = max(120, int(c.height))
        c.fps = max(1, int(c.fps))
        c.fourcc = str(c.fourcc or "MJPG").upper()
        if isinstance(c.match, str):
            c.match = [c.match]
        else:
            c.match = [str(item) for item in c.match]
        if isinstance(c.exclude, str):
            c.exclude = [c.exclude]
        else:
            c.exclude = [str(item) for item in c.exclude]
        if c.white_balance_temperature is not None:
            c.white_balance_temperature = max(2800, min(6500, int(c.white_balance_temperature)))
        if c.exposure is not None:
            c.exposure = max(1, min(5000, int(c.exposure)))

        p = self.processing
        p.buffer_size = max(1, min(100, int(p.buffer_size)))
        if p.linearize not in {"off", "rec601", "rec709", "srgb"}:
            p.linearize = "off"
        p.savgol_window = int(p.savgol_window) | 1
        p.savgol_window = max(3, min(41, p.savgol_window))
        p.savgol_order = max(0, min(5, int(p.savgol_order)))
        if p.savgol_window <= p.savgol_order:
            p.savgol_window = p.savgol_order + 2 | 1
        p.peak_threshold = max(0.0, min(1.0, float(p.peak_threshold)))
        p.peak_min_distance = max(1, min(200, int(p.peak_min_distance)))
        p.gain_r = max(0.0, float(p.gain_r))
        p.gain_g = max(0.0, float(p.gain_g))
        p.gain_b = max(0.0, float(p.gain_b))

        r = self.roi
        r.x = max(0, int(r.x))
        r.y = max(0, int(r.y))
        r.w = max(0, int(r.w))
        r.h = max(0, int(r.h))
        r.auto_height = max(1, min(200, int(r.auto_height)))

        d = self.display
        d.width = max(800, min(2560, int(d.width)))
        d.preview_height = max(220, min(500, int(d.preview_height)))
        d.graph_height = max(260, min(900, int(d.graph_height)))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


def load_config(path: str | Path, *, create: bool = True) -> tuple[AppConfig, Path]:
    path = Path(path)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON ({exc})") from exc
        config = AppConfig.from_dict(data)
    else:
        config = AppConfig()
        config.normalise()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            config.save(path)
    return config, path


def save_config(config: AppConfig, path: str | Path) -> Path:
    config.normalise()
    return config.save(path)
