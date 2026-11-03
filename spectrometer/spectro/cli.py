"""Command-line interface for the enzyflow PC spectrometer.

Examples
--------
List cameras and V4L2 controls::

    ./run.sh devices --controls

Live GUI with the USB spectrometer::

    ./run.sh run

Run without hardware (deterministic synthetic spectrum)::

    ./run.sh run --synthetic

Headless capture (averaged 30 frames)::

    ./run.sh capture --frames 30 --out runtime/captures/lab.csv

Calibrate from known lamp lines (absolute frame pixels)::

    ./run.sh calibrate --points "180:405.4, 600:546.5, 980:611.6"

Generate a tungsten reference CSV::

    ./run.sh reference-tungsten --temperature 2800 --out runtime/tungsten.csv
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import __version__
from .calibration import Calibration, CalibrationError
from .camera import (
    CameraSource,
    enumerate_devices,
    open_source,
    select_device,
)
from .config import AppConfig, default_data_dir, load_config
from .plot import render_spectrum
from .recording import append_jsonl, make_record, save_capture
from .reference import load_reference_csv, scaling_factors
from .roi import Roi, auto_roi, normalise_roi, parse_roi
from .spectrum import SpectrumProcessor, SpectrumResult
from .tungsten import save_tungsten_csv

__all__ = ["main"]


@dataclass
class Context:
    config: AppConfig
    config_path: Path
    data_dir: Path


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="config JSON (default: DATA_DIR/config.json)")
    parser.add_argument("--data-dir", type=Path, default=None, help="runtime directory (default: ./runtime)")


def _add_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", default=None, help="/dev/videoN, index, by-id or VID:PID")
    parser.add_argument("--synthetic", action="store_true", help="use a deterministic synthetic spectrum")
    parser.add_argument("--video", default=None, help="read a recorded video or still image instead of a camera")
    parser.add_argument("--width", type=int, default=None, help="requested frame width")
    parser.add_argument("--height", type=int, default=None, help="requested frame height")
    parser.add_argument("--fps", type=int, default=None, help="requested frame rate")
    parser.add_argument("--fourcc", default=None, help="requested FOURCC (default MJPG)")
    parser.add_argument("--calibration", type=Path, default=None, help="calibration JSON (default DATA_DIR/calibration.json)")
    parser.add_argument("--no-calibration", action="store_true", help="ignore the saved calibration")
    parser.add_argument("--exposure", type=int, default=None, help="manual exposure (V4L2 100 us units)")
    parser.add_argument("--auto-exposure", action="store_true", help="force automatic exposure")


def _prepare(args: argparse.Namespace) -> Context:
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser() if args.config else data_dir / "config.json"
    config_path = config_path.resolve()
    config, config_path = load_config(config_path)

    if getattr(args, "width", None) is not None:
        config.camera.width = int(args.width)
    if getattr(args, "height", None) is not None:
        config.camera.height = int(args.height)
    if getattr(args, "fps", None) is not None:
        config.camera.fps = int(args.fps)
    if getattr(args, "fourcc", None):
        config.camera.fourcc = str(args.fourcc)
    if getattr(args, "auto_exposure", False):
        config.camera.exposure_auto = True
    if getattr(args, "exposure", None) is not None:
        config.camera.exposure_auto = False
        config.camera.exposure = int(args.exposure)
    config.normalise()
    return Context(config, config_path, data_dir)


def _load_calibration(args: argparse.Namespace, data_dir: Path) -> Calibration | None:
    if getattr(args, "no_calibration", False):
        return None
    path = Path(args.calibration).expanduser() if getattr(args, "calibration", None) else data_dir / "calibration.json"
    if not path.exists():
        return None
    try:
        calibration = Calibration.load(path)
    except (OSError, ValueError, CalibrationError) as exc:
        print(f"[cal] could not load {path}: {exc}")
        return None
    print(f"[cal] loaded {path} ({calibration.describe()})")
    return calibration


def _apply_source_config(source: Any, config: AppConfig) -> None:
    if not isinstance(source, CameraSource):
        return
    try:
        if config.camera.exposure_auto:
            source.set_exposure_auto(True)
        elif config.camera.exposure is not None:
            source.set_exposure(config.camera.exposure)
        else:
            source.set_exposure_auto(True)
    except Exception as exc:
        print(f"[camera] exposure setup failed: {exc}")


def _open(args: argparse.Namespace, context: Context):
    source = open_source(
        context.config.camera,
        explicit_device=getattr(args, "device", None),
        synthetic=getattr(args, "synthetic", False),
        video=getattr(args, "video", None),
    )
    _apply_source_config(source, context.config)
    print(f"[camera] {source.info}")
    return source


def _roi_for(args: argparse.Namespace, config: AppConfig, frame: np.ndarray, first: bool) -> Roi:
    height, width = frame.shape[:2]
    text = getattr(args, "roi", None)
    if text:
        return parse_roi(text, width, height)
    if first and (config.roi.auto or config.roi.w <= 0 or config.roi.h <= 0):
        roi = auto_roi(frame, config.roi.auto_height)
        print(f"[roi] auto: x={roi.x} y={roi.y} w={roi.w} h={roi.h}")
        return roi
    if config.roi.w > 0 and config.roi.h > 0:
        return normalise_roi(Roi(config.roi.x, config.roi.y, config.roi.w, config.roi.h), width, height)
    return auto_roi(frame, config.roi.auto_height)


def _finish_result(
    result: SpectrumResult,
    calibration: Calibration | None,
    roi: Roi,
) -> SpectrumResult:
    if calibration is not None:
        result.wavelengths = calibration.wavelengths(roi.x, roi.w)
    return result


def _exposure_meta(source: Any, config: AppConfig) -> dict:
    value = config.camera.exposure
    if isinstance(source, CameraSource):
        try:
            current = source.get_exposure()
            if current is not None:
                value = current
        except Exception:
            pass
    return {
        "auto": bool(config.camera.exposure_auto),
        "value": int(value) if value is not None else None,
        "units": "V4L2 exposure_time_absolute (100 us units on this camera)",
    }


def _write_outputs(
    args: argparse.Namespace,
    context: Context,
    source: Any,
    result: SpectrumResult,
    calibration: Calibration | None,
    roi: Roi,
    graph: np.ndarray | None = None,
    frame: np.ndarray | None = None,
) -> dict[str, str]:
    out = getattr(args, "out", None)
    jsonl = getattr(args, "jsonl", None)
    files: dict[str, str] = {}
    if out:
        out_path = Path(out).expanduser()
        if out_path.suffix.lower() == ".jsonl":
            if jsonl is None:
                jsonl = out_path
        else:
            if out_path.suffix.lower() in {".csv", ".json", ".png"}:
                out_dir = out_path.parent
                stem = out_path.stem
            else:
                out_dir = out_path
                stem = None
            files = save_capture(
                out_dir,
                result,
                calibration=calibration,
                roi=roi,
                source_info=source.info,
                exposure=_exposure_meta(source, context.config),
                note="headless capture",
                graph_image=graph,
                frame=frame,
                save_frame=context.config.export.save_frame and not getattr(args, "no_frame", False),
                save_ui=False,
                stem=stem,
            )
    else:
        files = save_capture(
            context.data_dir / context.config.export.directory,
            result,
            calibration=calibration,
            roi=roi,
            source_info=source.info,
            exposure=_exposure_meta(source, context.config),
            note="headless capture",
            graph_image=graph,
            frame=frame,
            save_frame=context.config.export.save_frame and not getattr(args, "no_frame", False),
            save_ui=False,
        )

    if jsonl:
        record = make_record(
            result,
            calibration=calibration,
            roi=roi,
            source_info=source.info,
            exposure=_exposure_meta(source, context.config),
            event="capture",
            note="headless capture",
            include_arrays=True,
        )
        record["files"] = files
        append_jsonl(jsonl, record)
        files["jsonl"] = str(Path(jsonl))
    return files


def _read_frame(source: Any, retries: int = 20) -> np.ndarray:
    for _ in range(retries):
        frame = source.read()
        if frame is not None:
            return frame
        time.sleep(0.05)
    raise RuntimeError("camera/source did not return a frame")


def cmd_devices(args: argparse.Namespace) -> int:
    context = _prepare(args)
    devices = enumerate_devices()
    print(f"Enzyflow spectrometer {__version__}")
    print(f"config: {context.config_path}")
    if not devices:
        print("no /dev/video* devices found")
        return 1
    for device in devices:
        marker = "capture" if device.capture else "metadata"
        print(f"  {device.path:14s} {marker:8s} {device.vid_pid:10s} {device.name}")
        if device.by_id:
            print(f"                 by-id: {device.by_id}")
    try:
        selected = select_device(context.config.camera, args.device)
        print(f"\nselected: {selected.path}  {selected.name}  [{selected.vid_pid}]")
    except RuntimeError as exc:
        print(f"\n[error] {exc}")
        return 1
    if args.controls:
        try:
            with CameraSource(
                selected.path,
                width=context.config.camera.width,
                height=context.config.camera.height,
                fps=context.config.camera.fps,
                fourcc=context.config.camera.fourcc,
            ) as camera:
                print("\ncontrols:")
                for name, values in camera.controls().items():
                    print(
                        f"  {name:28s} value={values.get('value', '?'):>6} "
                        f"min={values.get('min', '?'):>6} max={values.get('max', '?'):>6} "
                        f"{values.get('flags', '')}"
                    )
        except Exception as exc:
            print(f"[error] could not read controls: {exc}")
            return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    context = _prepare(args)
    print(f"Enzyflow spectrometer {__version__}")
    print(f"python      {sys.version.split()[0]}")
    print(f"numpy       {np.__version__}")
    print(f"opencv      {cv2.__version__}")
    print(f"v4l2-ctl    {shutil.which('v4l2-ctl') or 'not found'}")
    print(f"session     {os.environ.get('XDG_SESSION_TYPE', '?')}  DISPLAY={os.environ.get('DISPLAY', '')}  WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')}")
    print(f"data dir    {context.data_dir}")
    print(f"config      {context.config_path}")
    cal = context.data_dir / "calibration.json"
    print(f"calibration {'present' if cal.exists() else 'not present'}  {cal}")
    devices = enumerate_devices()
    print(f"video nodes {len(devices)}")
    for device in devices:
        print(f"  {device.path:14s} {'capture' if device.capture else 'metadata':8s} {device.vid_pid:10s} {device.name}")
    try:
        selected = select_device(context.config.camera, getattr(args, "device", None))
        print(f"selected    {selected.path}  {selected.name}  [{selected.vid_pid}]")
    except RuntimeError as exc:
        print(f"selected    [error] {exc}")
    for path in (context.data_dir / "captures",):
        print(f"captures    {path} {'present' if path.exists() else 'missing'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    context = _prepare(args)
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and not args.render_once:
        print("[error] no graphical display. Use 'capture' for headless operation.")
        return 1
    calibration = _load_calibration(args, context.data_dir)
    source = _open(args, context)
    try:
        from .app import SpectrometerApp

        app = SpectrometerApp(
            context.config,
            context.config_path,
            context.data_dir,
            source,
            calibration,
            waterfall=args.waterfall,
            fullscreen=args.fullscreen,
            render_once=Path(args.render_once).expanduser() if args.render_once else None,
        )
        return app.run()
    finally:
        source.close()


def _parse_point_tokens(text: str) -> list[tuple[int, float]]:
    # Accept "123:405.4, 456:546.5", "123,405.4 456,546.5" and
    # whitespace-separated "123 405.4  456 546.5".
    matches = re.findall(r"(-?\d+)\s*(?:[:,]|\s)\s*(-?\d+(?:\.\d+)?)", text)
    points = [(int(pixel), float(wavelength)) for pixel, wavelength in matches]
    if not points:
        raise ValueError("no pixel:wavelength pairs found")
    return points


def cmd_calibrate(args: argparse.Namespace) -> int:
    context = _prepare(args)
    if args.from_caldata:
        calibration = Calibration.from_caldata_file(Path(args.from_caldata).expanduser(), roi_x=args.roi_x)
    else:
        points: list[tuple[int, float]] = []
        if args.points:
            points.extend(_parse_point_tokens(args.points))
        if args.points_file:
            for line in Path(args.points_file).expanduser().read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                points.extend(_parse_point_tokens(line))
        if not points and not sys.stdin.isatty():
            for line in sys.stdin:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                points.extend(_parse_point_tokens(line))
        if not points:
            print("[error] provide --points, --points-file or --from-caldata")
            return 1
        points = [(pixel + args.roi_x, wavelength) for pixel, wavelength in points] if args.roi_x else points
        calibration = Calibration.fit(
            points,
            degree=args.degree,
            note=args.note,
            frame_width=args.frame_width,
        )

    out = Path(args.out).expanduser() if args.out else context.data_dir / "calibration.json"
    calibration.save(out)
    print(f"[cal] saved {out}")
    print(f"[cal] {calibration.describe()}")
    print(f"[cal] coefficients (highest order first): {calibration.coefficients}")
    print(f"[cal] id: {calibration.calibration_id}")
    if args.caldata_out:
        path = calibration.save_caldata(Path(args.caldata_out).expanduser(), roi_x=args.roi_x)
        print(f"[cal] PySpectrometer2 caldata: {path}")
    return 0


def _stream_capture(
    args: argparse.Namespace,
    context: Context,
    source: Any,
    calibration: Calibration | None,
    roi: Roi,
) -> int:
    jsonl = getattr(args, "jsonl", None)
    if not jsonl:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        jsonl = context.data_dir / context.config.export.directory / f"stream-{stamp}.jsonl"
    jsonl = Path(jsonl).expanduser()
    end = time.monotonic() + float(args.duration)
    interval = max(0.01, float(args.interval))
    count = 0
    processor = SpectrumProcessor(context.config.processing)
    print(f"[stream] writing {jsonl} for {args.duration}s at {interval:.2f}s intervals")
    while time.monotonic() < end:
        started = time.monotonic()
        frame = _read_frame(source)
        if roi.x + roi.w > frame.shape[1] or roi.y + roi.h > frame.shape[0]:
            roi = _roi_for(args, context.config, frame, first=(count == 0))
        crop = roi.crop(frame)
        result = _finish_result(processor.update(crop, roi.x), calibration, roi)
        if args.reference_csv:
            reference = load_reference_csv(args.reference_csv)
            factors = scaling_factors(result.wavelengths, result.intensity, *reference)
            result.intensity = (result.intensity * factors).astype(np.float32)
            result.display = (result.display * factors).astype(np.float32)
        record = make_record(
            result,
            calibration=calibration,
            roi=roi,
            source_info=source.info,
            exposure=_exposure_meta(source, context.config),
            seq=count,
            event="measurement",
            note="stream capture",
            include_arrays=True,
        )
        if args.reference_csv:
            record["reference_csv"] = str(Path(args.reference_csv).expanduser())
        append_jsonl(jsonl, record)
        count += 1
        elapsed = time.monotonic() - started
        if elapsed < interval:
            time.sleep(interval - elapsed)
    print(f"[stream] wrote {count} records to {jsonl}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    context = _prepare(args)
    calibration = _load_calibration(args, context.data_dir)
    source = _open(args, context)
    try:
        frame = _read_frame(source)
        roi = _roi_for(args, context.config, frame, first=True)
        if args.duration and args.duration > 0:
            return _stream_capture(args, context, source, calibration, roi)

        warmup = max(0, int(args.warmup))
        for _ in range(warmup):
            _read_frame(source)

        frames = max(1, int(args.frames))
        context.config.processing.buffer_size = frames
        processor = SpectrumProcessor(context.config.processing)
        result: SpectrumResult | None = None
        last_frame = frame
        for _ in range(frames):
            last_frame = _read_frame(source)
            result = processor.update(roi.crop(last_frame), roi.x)
        assert result is not None
        result = _finish_result(result, calibration, roi)

        if args.reference_csv:
            reference = load_reference_csv(args.reference_csv)
            if result.wavelengths is None:
                print("[error] --reference-csv needs a wavelength calibration")
                return 1
            factors = scaling_factors(result.wavelengths, result.intensity, *reference)
            result.intensity = (result.intensity * factors).astype(np.float32)
            result.display = (result.display * factors).astype(np.float32)

        graph = render_spectrum(
            result,
            calibration=calibration,
            roi=roi,
            peak_threshold=context.config.processing.peak_threshold,
            peak_min_distance=context.config.processing.peak_min_distance,
        )
        files = _write_outputs(args, context, source, result, calibration, roi, graph=graph, frame=last_frame)
        print(f"[capture] roi x={roi.x} y={roi.y} w={roi.w} h={roi.h} frames={frames}")
        print(f"[capture] peak {result.peak_wavelength:.2f} nm" if result.peak_wavelength is not None else f"[capture] peak px {int(result.pixels[result.peak_index])}")
        for key, value in files.items():
            print(f"[capture] {key}: {value}")
        return 0
    finally:
        source.close()


def cmd_reference_tungsten(args: argparse.Namespace) -> int:
    context = _prepare(args)
    out = Path(args.out).expanduser() if args.out else context.data_dir / "tungsten.csv"
    save_tungsten_csv(out, filament_temp=args.temperature, low_nm=args.low, high_nm=args.high)
    print(f"[reference] tungsten {args.temperature} K -> {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spectro",
        description="Enzyflow PC-side USB spectrometer (OpenCV + NumPy)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="live OpenCV GUI")
    _add_common(p_run)
    _add_source(p_run)
    p_run.add_argument("--waterfall", action="store_true", help="start with the waterfall window")
    p_run.add_argument("--fullscreen", action="store_true")
    p_run.add_argument("--render-once", type=Path, default=None, help="render one synthetic/real frame to PNG and exit")
    p_run.set_defaults(func=cmd_run)

    p_cap = sub.add_parser("capture", help="headless capture to CSV/JSON/JSONL")
    _add_common(p_cap)
    _add_source(p_cap)
    p_cap.add_argument("--frames", type=int, default=10, help="frames to average (default 10)")
    p_cap.add_argument("--warmup", type=int, default=5, help="frames to discard before capture")
    p_cap.add_argument("--out", default=None, help="output directory or .csv/.json/.jsonl/.png prefix")
    p_cap.add_argument("--jsonl", default=None, help="also append a JSONL record to this file")
    p_cap.add_argument("--roi", default=None, help="ROI as x,y,w,h (absolute frame pixels)")
    p_cap.add_argument("--reference-csv", default=None, help="apply a reference-response CSV correction")
    p_cap.add_argument("--duration", type=float, default=0.0, help="stream for N seconds instead of one capture")
    p_cap.add_argument("--interval", type=float, default=1.0, help="stream interval in seconds")
    p_cap.add_argument("--no-frame", action="store_true", help="do not save the raw frame PNG")
    p_cap.set_defaults(func=cmd_capture)

    p_cal = sub.add_parser("calibrate", help="fit pixel -> wavelength points and save calibration")
    _add_common(p_cal)
    p_cal.add_argument("--points", default=None, help='e.g. "180:405.4, 600:546.5, 980:611.6"')
    p_cal.add_argument("--points-file", default=None, help="text file with pixel,wavelength per line")
    p_cal.add_argument("--from-caldata", default=None, help="import PySpectrometer2 caldata.txt")
    p_cal.add_argument("--roi-x", type=int, default=0, help="add this ROI x offset to imported/relative pixels")
    p_cal.add_argument("--degree", type=int, default=None, help="polynomial degree (default: 2 for 3 points, 3 otherwise)")
    p_cal.add_argument("--frame-width", type=int, default=None)
    p_cal.add_argument("--note", default="")
    p_cal.add_argument("--out", default=None, help="output calibration JSON")
    p_cal.add_argument("--caldata-out", default=None, help="also write PySpectrometer2 caldata.txt")
    p_cal.set_defaults(func=cmd_calibrate)

    p_dev = sub.add_parser("devices", help="list V4L2 devices and optionally controls")
    _add_common(p_dev)
    p_dev.add_argument("--device", default=None)
    p_dev.add_argument("--controls", action="store_true")
    p_dev.set_defaults(func=cmd_devices)

    p_doc = sub.add_parser("doctor", help="show environment/device diagnostics")
    _add_common(p_doc)
    p_doc.add_argument("--device", default=None)
    p_doc.set_defaults(func=cmd_doctor)

    p_t = sub.add_parser("reference-tungsten", help="generate a tungsten reference CSV")
    _add_common(p_t)
    p_t.add_argument("--temperature", type=int, default=2800)
    p_t.add_argument("--low", type=int, default=340)
    p_t.add_argument("--high", type=int, default=2000)
    p_t.add_argument("--out", default=None)
    p_t.set_defaults(func=cmd_reference_tungsten)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["run"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except (RuntimeError, ValueError, CalibrationError, OSError, cv2.error) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[exit] interrupted")
        return 130
