"""Enzyflow PC-side USB spectrometer toolkit.

A small, readable Linux-native implementation inspired by:

* PySpectrometer2 (Les Wright, Apache-2.0)
* spectro-cam-rs (DerFetzer, Apache-2.0 / MIT)
* the general Theremino Spectrometer workflow (pixel -> wavelength
  calibration, live spectrum, absorption/zero reference).

The code is intentionally dependency-light (NumPy + OpenCV) so it can be
studied, modified and re-used for secondary development.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
