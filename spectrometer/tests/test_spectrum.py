import unittest

import numpy as np

from spectro.config import ProcessingConfig
from spectro.spectrum import SpectrumProcessor, linearise


class SpectrumTests(unittest.TestCase):
    def test_extract_constant_colour(self):
        crop = np.zeros((10, 100, 3), dtype=np.uint8)
        crop[:, :, 0] = 10  # B
        crop[:, :, 1] = 20  # G
        crop[:, :, 2] = 30  # R
        processor = SpectrumProcessor(ProcessingConfig(buffer_size=1, savgol_enabled=False))
        result = processor.update(crop, pixel_start=5)
        self.assertEqual(result.pixels[0], 5)
        self.assertAlmostEqual(float(result.b[0]), 10.0, places=4)
        self.assertAlmostEqual(float(result.g[0]), 20.0, places=4)
        self.assertAlmostEqual(float(result.r[0]), 30.0, places=4)
        self.assertAlmostEqual(float(result.intensity[0]), 20.0, places=4)
        self.assertFalse(result.saturated)

    def test_gain_and_buffer_average(self):
        cfg = ProcessingConfig(buffer_size=2, savgol_enabled=False, gain_r=2.0)
        processor = SpectrumProcessor(cfg)
        frame1 = np.full((4, 50, 3), 10, dtype=np.uint8)
        frame2 = np.full((4, 50, 3), 20, dtype=np.uint8)
        processor.update(frame1)
        result = processor.update(frame2)
        # Average (10,10,10) and (20,20,20) = 15; red gain doubles red.
        self.assertAlmostEqual(float(result.r[0]), 30.0, places=4)
        self.assertAlmostEqual(float(result.g[0]), 15.0, places=4)
        self.assertAlmostEqual(float(result.intensity[0]), 20.0, places=4)

    def test_zero_reference(self):
        cfg = ProcessingConfig(buffer_size=1, savgol_enabled=False)
        processor = SpectrumProcessor(cfg)
        frame = np.full((4, 50, 3), 40, dtype=np.uint8)
        processor.update(frame)
        self.assertTrue(processor.set_zero_reference())
        second = processor.update(frame)
        self.assertTrue(second.zero_active)
        self.assertAlmostEqual(float(second.intensity[0]), 0.0, places=4)
        processor.clear_zero_reference()
        third = processor.update(frame)
        self.assertAlmostEqual(float(third.intensity[0]), 40.0, places=4)

    def test_hold_peaks(self):
        cfg = ProcessingConfig(buffer_size=1, savgol_enabled=False, hold_peaks=True)
        processor = SpectrumProcessor(cfg)
        low = np.full((4, 50, 3), 20, dtype=np.uint8)
        high = np.full((4, 50, 3), 80, dtype=np.uint8)
        processor.update(low)
        result = processor.update(high)
        self.assertAlmostEqual(float(result.display[0]), 80.0, places=4)
        result2 = processor.update(low)
        self.assertAlmostEqual(float(result2.display[0]), 80.0, places=4)

    def test_saturation(self):
        cfg = ProcessingConfig(buffer_size=1, savgol_enabled=False)
        processor = SpectrumProcessor(cfg)
        crop = np.full((10, 100, 3), 255, dtype=np.uint8)
        result = processor.update(crop)
        self.assertTrue(result.saturated)
        self.assertGreater(result.saturated_ratio, 0.9)

    def test_width_change_clears_buffer(self):
        cfg = ProcessingConfig(buffer_size=5, savgol_enabled=False)
        processor = SpectrumProcessor(cfg)
        processor.update(np.full((4, 100, 3), 10, dtype=np.uint8))
        self.assertEqual(len(processor._buffer), 1)
        processor.update(np.full((4, 80, 3), 10, dtype=np.uint8))
        self.assertEqual(len(processor._buffer), 1)

    def test_linearise(self):
        self.assertAlmostEqual(float(linearise(np.array([0.0]), "srgb")[0]), 0.0)
        self.assertAlmostEqual(float(linearise(np.array([255.0]), "srgb")[0]), 255.0, places=3)
        x = np.array([0.0, 64.0, 128.0, 255.0])
        y = linearise(x, "rec709")
        self.assertTrue(np.all(np.diff(y) > 0))


if __name__ == "__main__":
    unittest.main()
