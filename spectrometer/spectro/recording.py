"""CSV / JSON / JSONL capture writers.

The CSV is deliberately simple (one row per pixel) so it opens in pandas,
LibreOffice, Origin, MATLAB, etc.  A sidecar JSON records instrument state,
ROI, exposure, calibration and file names.  JSONL is provided for live
integration with the ESP32/AS7341 collector: one object per line can be
appended without rewriting the whole file.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .calibration import Calibration
from .roi import Roi
from .spectrum import SpectrumResult

__all__ = ["save_capture", "make_record", "append_jsonl", "save_csv"]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    return value


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free file name next to {path}")


def _timestamp() -> tuple[str, str]:
    now = datetime.now().astimezone()
    return now.strftime("%Y%m%d-%H%M%S"), now.isoformat(timespec="milliseconds")


def save_csv(path: str | Path, result: SpectrumResult, photometry=None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    wavelengths = result.wavelengths
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "pixel",
                "wavelength_nm",
                "r",
                "g",
                "b",
                "intensity",
                "display",
                "luminance",
                "raw_intensity",
                "dark_background",
                "transmittance", "absorbance", "reference_id",
            ]
        )
        for i in range(result.intensity.size):
            writer.writerow(
                [
                    int(result.pixels[i]),
                    f"{float(wavelengths[i]):.4f}" if wavelengths is not None else "",
                    f"{float(result.r[i]):.6g}",
                    f"{float(result.g[i]):.6g}",
                    f"{float(result.b[i]):.6g}",
                    f"{float(result.intensity[i]):.6g}",
                    f"{float(result.display[i]):.6g}",
                    f"{float(result.luma[i]):.6g}",
                    f"{float(result.raw_intensity[i]):.6g}" if result.raw_intensity is not None else "",
                    f"{float(result.dark_profile[i]):.6g}" if result.dark_profile is not None else "",
                    photometry['transmittance'][i] if photometry and photometry.get('active') else '',
                    photometry['absorbance'][i] if photometry and photometry.get('active') else '',
                    photometry.get('reference_id','') if photometry else '',
                ]
            )
    tmp.replace(path)
    return path


def make_record(
    result: SpectrumResult,
    *,
    calibration: Calibration | None,
    roi: Roi,
    source_info: dict,
    exposure: dict,
    seq: int = 0,
    event: str = "measurement",
    note: str = "",
    include_arrays: bool = True,
) -> dict:
    record: dict[str, Any] = {
        "schema": 1,
        "source": "pc.usb_spectrometer",
        "device_id": source_info.get("path", "unknown"),
        "seq": int(seq),
        "t_monotonic_ms": int(time.monotonic() * 1000),
        "t_wall": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "event": event,
        "note": note,
        "camera": source_info,
        "roi": {
            "x": int(roi.x),
            "y": int(roi.y),
            "w": int(roi.w),
            "h": int(roi.h),
        },
        "exposure": exposure,
        "calibration": calibration.to_dict() if calibration else None,
        "dark_correction": {
            "active": result.dark_profile is not None,
            "metadata": result.dark_metadata,
            "background": result.dark_profile.tolist() if result.dark_profile is not None else None,
            "method": "intensity = raw_intensity - mean_dark - optional_zero; negatives retained; RGB unchanged",
        },
        "units": {
            "wavelength": "nm",
            "intensity": "camera counts, gain/dark/zero corrected as indicated; signed, not radiometrically calibrated",
        },
        "stats": result.summary(),
        "files": {},
    }
    if include_arrays:
        record["pixel"] = [int(v) for v in result.pixels]
        record["wavelength_nm"] = (
            [round(float(v), 4) for v in result.wavelengths] if result.wavelengths is not None else None
        )
        record["intensity"] = [round(float(v), 5) for v in result.intensity]
        record["raw_intensity"] = result.raw_intensity.tolist() if result.raw_intensity is not None else None
        record["display"] = [round(float(v), 5) for v in result.display]
        record["r"] = [round(float(v), 5) for v in result.r]
        record["g"] = [round(float(v), 5) for v in result.g]
        record["b"] = [round(float(v), 5) for v in result.b]
        record["luminance"] = [round(float(v), 5) for v in result.luma]
    return record


def append_jsonl(path: str | Path, record: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(record), ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
    return path


def save_capture(
    out_dir: str | Path,
    result: SpectrumResult,
    *,
    calibration: Calibration | None,
    roi: Roi,
    source_info: dict,
    exposure: dict,
    seq: int = 0,
    note: str = "",
    graph_image: np.ndarray | None = None,
    ui_image: np.ndarray | None = None,
    frame: np.ndarray | None = None,
    save_frame: bool = True,
    save_ui: bool = True,
    stem: str | None = None,
    extra: dict | None = None,
) -> dict[str, str]:
    """Write CSV + JSON metadata + optional PNGs.  Returns the file paths.

    ``stem`` allows the caller to request a specific file prefix; otherwise a
    timestamped ``spectrum-YYYYmmdd-HHMMSS`` base name is used.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp, iso = _timestamp()
    base = _unique_path(out_dir / (stem or f"spectrum-{stamp}"))
    csv_path = base.with_suffix(".csv")
    json_path = base.with_suffix(".json")
    png_path = base.with_suffix(".png")
    ui_path = out_dir / f"{base.name}-ui.png"
    frame_path = out_dir / f"{base.name}-frame.png"

    save_csv(csv_path, result, (extra or {}).get("photometry"))
    record = make_record(
        result,
        calibration=calibration,
        roi=roi,
        source_info=source_info,
        exposure=exposure,
        seq=seq,
        event="capture",
        note=note,
        include_arrays=False,
    )
    record.update(extra or {})
    record["t_wall"] = iso
    files: dict[str, str] = {"csv": str(csv_path), "json": str(json_path)}
    if graph_image is not None:
        if cv2.imwrite(str(png_path), graph_image):
            files["graph_png"] = str(png_path)
    if ui_image is not None and save_ui:
        if cv2.imwrite(str(ui_path), ui_image):
            files["ui_png"] = str(ui_path)
    if frame is not None and save_frame:
        if cv2.imwrite(str(frame_path), frame):
            files["frame_png"] = str(frame_path)
    record["files"] = files

    tmp = json_path.with_suffix(json_path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(record), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(json_path)
    return files
