import tempfile
import unittest
from pathlib import Path

import numpy as np

from spectro.calibration import Calibration, CalibrationError, generate_graticule


def _quadratic(x: float) -> float:
    return 380.0 + 0.30 * x + 1.0e-5 * x * x


class CalibrationTests(unittest.TestCase):
    def test_four_points_use_third_order_and_recover_quadratic(self):
        points = [(x, _quadratic(x)) for x in (80, 320, 640, 960, 1240)]
        calibration = Calibration.fit(points)
        self.assertEqual(calibration.degree, 3)
        self.assertGreater(calibration.r_squared, 0.999999)
        predicted = calibration.wavelengths(0, 1280)
        expected = _quadratic(np.arange(1280))
        self.assertLess(float(np.max(np.abs(predicted - expected))), 0.05)

    def test_three_points_use_second_order(self):
        calibration = Calibration.fit([(0, 400), (600, 560), (1200, 720)])
        self.assertEqual(calibration.degree, 2)
        self.assertAlmostEqual(calibration.wavelength_at(600), 560.0, places=6)

    def test_save_load_roundtrip(self):
        calibration = Calibration.fit([(100, 405.4), (500, 546.5), (900, 611.6)], note="lab")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration.json"
            calibration.save(path)
            loaded = Calibration.load(path)
        self.assertEqual(loaded.points, calibration.points)
        self.assertAlmostEqual(loaded.wavelength_at(500), calibration.wavelength_at(500))
        self.assertEqual(loaded.calibration_id, calibration.calibration_id)

    def test_pyspectrometer_caldata_roundtrip(self):
        calibration = Calibration.fit([(100, 405.4), (500, 546.5), (900, 611.6)])
        text = calibration.to_caldata_text(roi_x=50)
        imported = Calibration.from_caldata_text(text, roi_x=50)
        self.assertEqual(imported.points, calibration.points)

    def test_graticule_is_within_axis(self):
        calibration = Calibration.fit([(0, 400), (640, 560), (1279, 720)])
        tens, fifties = generate_graticule(calibration, 0, 1280)
        self.assertTrue(tens)
        self.assertTrue(fifties)
        self.assertTrue(all(0 <= index < 1280 for index, _ in tens))
        self.assertTrue(all(nm % 50 == 0 for _, nm in fifties))

    def test_rejects_too_few_or_duplicate_points(self):
        with self.assertRaises(CalibrationError):
            Calibration.fit([(0, 400), (10, 410)])
        with self.assertRaises(CalibrationError):
            Calibration.fit([(10, 400), (10, 401), (20, 410)])

    def test_points_in_roi(self):
        calibration = Calibration.fit([(100, 400), (500, 500), (900, 600)])
        self.assertEqual(calibration.points_in_roi(90, 30), 1)
        self.assertEqual(calibration.points_in_roi(500, 100), 1)


if __name__ == "__main__":
    unittest.main()
