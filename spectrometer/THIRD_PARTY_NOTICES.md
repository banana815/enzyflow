# Third-party notices and algorithm provenance

This project is an independent Python/OpenCV implementation for the enzyflow
USB spectrometer.  It was written after studying the following projects; no
complete upstream source file was copied into `spectro/`.

## PySpectrometer2

- Author: Les Wright
- Source: https://github.com/leswright1977/PySpectrometer2
- License: Apache License 2.0

Concepts used:

- USB/V4L2 camera capture and key binding style (`t/g`, `o/l`, `i/k`,
  `u/j`, `h`, `c`, `p`, `x`, `s`, `q`);
- 3-row/ROI averaging of camera columns;
- 3-point quadratic and 4+-point cubic wavelength calibration, including the
  `caldata.txt` two-line format;
- peak hold, peak labelling, Savitzky-Golay smoothing, wavelength-coloured
  graph, waterfall display;
- `wavelength_to_rgb` display approximation (originally by Chris Webb,
  "Exploring the visible spectrum in Python").
- `specFunctions.py` also contains a SciPy-cookbook Savitzky-Golay recipe
  (BSD-style, SciPy Developers) and a peak finder derived from peakutils
  (MIT, Lucas Hermann Negri).  The versions in `spectro/peaks.py` are
  rewritten for NumPy 2 and this project's simpler API; attribution remains.

## spectro-cam-rs

- Author: DerFetzer
- Source: https://github.com/DerFetzer/spectro-cam-rs
- License: Apache-2.0 OR MIT

Concepts used:

- adjustable camera window / ROI and per-channel RGB gains;
- sRGB / Rec.601 / Rec.709 linearisation;
- frame averaging buffer, low-pass/zero-reference style processing;
- reference spectrum calibration (`reference / measured` scaling);
- tungsten filament reference spectrum, ported from
  `src/tungsten_halogen.rs` (emissivity model from de Vos et al., Applied
  Optics 23 (1984) 975);
- peak/dip local-window detection and CSV export fields.

## Theremino Spectrometer

- Website: https://www.theremino.com/downloads/automation
- Public source copies are commonly distributed under GPL-3.0; confirm the
  exact license in the copy you have.

Only functional concepts were used; no Theremino source code is included.
If you port GPL Theremino code into this project, the combined work must
comply with GPL-3.0.  See `docs/VBNET_TO_PYTHON.md`.

## OpenCV

- https://opencv.org/
- Apache License 2.0
- Used for V4L2 capture, image resizing, drawing, PNG writing.

## NumPy

- https://numpy.org/
- BSD-3-Clause
- Used for arrays, polynomial fitting, interpolation and numerical helpers.

## Notes on data and accuracy

The camera is an 8-bit UVC device.  The software does not claim absolute
radiometric calibration, CIE color accuracy, or a specific wavelength
accuracy until the user performs a calibration with known spectral lines.
