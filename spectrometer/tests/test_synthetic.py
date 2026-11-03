import unittest

import numpy as np

from spectro.camera import SyntheticSource
from spectro.config import ProcessingConfig
from spectro.roi import auto_roi, normalise_roi, parse_roi
from spectro.roi import Roi as RoiClass
from spectro.spectrum import SpectrumProcessor


class RoiTests(unittest.TestCase):
    def test_parse_and_clamp(self):
        roi = parse_roi("10,20,300,4", 100, 100)
        self.assertEqual(roi.as_tuple(), (10, 20, 90, 4))
        roi = normalise_roi(RoiClass(-5, -5, 200, 200), 100, 100)
        self.assertEqual(roi.as_tuple(), (0, 0, 100, 100))

    def test_auto_roi_finds_synthetic_band(self):
        source = SyntheticSource(1280, 720)
        frame = source.read()
        roi = auto_roi(frame, 8)
        self.assertGreater(roi.w, 800)
        self.assertGreaterEqual(roi.y, 300)
        self.assertLessEqual(roi.y + roi.h, 420)
        self.assertLessEqual(roi.h, 40)

    def test_auto_roi_dark_frame_falls_back_to_centre(self):
        frame = np.zeros((200, 300, 3), dtype=np.uint8)
        roi = auto_roi(frame, 8)
        self.assertEqual(roi.w, 300)
        self.assertAlmostEqual(roi.y + roi.h / 2, 100, delta=2)


class SyntheticTests(unittest.TestCase):
    def test_known_lines_are_near_expected_pixels(self):
        source = SyntheticSource(1280, 720)
        frame = source.read()
        roi = auto_roi(frame, 8)
        processor = SpectrumProcessor(
            ProcessingConfig(buffer_size=1, savgol_enabled=False, peak_min_distance=20)
        )
        result = processor.update(roi.crop(frame), roi.x)
        # The strongest synthetic line is 546.5 nm at x ~= 580.
        peak_pixel = int(result.pixels[result.peak_index])
        self.assertAlmostEqual(peak_pixel, 580, delta=8)

    def test_wavelength_mapping_is_quadratic(self):
        x = np.array([0.0, 640.0, 1279.0])
        wl = SyntheticSource.wavelength_at(x, 1280)
        self.assertAlmostEqual(float(wl[0]), 380.0, places=3)
        self.assertTrue(550 < float(wl[1]) < 590)
        self.assertTrue(750 < float(wl[2]) < 770)


if __name__ == "__main__":
    unittest.main()
