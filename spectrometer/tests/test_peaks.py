import unittest

import numpy as np

from spectro.peaks import find_peaks, find_peaks_dips, savitzky_golay


class PeakTests(unittest.TestCase):
    def test_savitzky_golay_preserves_linear_signal(self):
        y = np.linspace(0.0, 10.0, 50)
        filtered = savitzky_golay(y, 9, 3)
        self.assertEqual(filtered.shape, y.shape)
        self.assertLess(float(np.max(np.abs(filtered - y))), 1e-6)

    def test_find_peaks(self):
        y = np.zeros(500)
        for center, height in ((100, 50.0), (200, 100.0), (400, 75.0)):
            x = np.arange(500)
            y += height * np.exp(-0.5 * ((x - center) / 3.0) ** 2)
        peaks = find_peaks(y, threshold=0.1, min_distance=20)
        self.assertEqual(peaks.tolist(), [100, 200, 400])

    def test_min_distance_suppresses_nearby_peak(self):
        y = np.zeros(100)
        y[30] = 10
        y[35] = 20
        self.assertEqual(find_peaks(y, threshold=0.01, min_distance=10).tolist(), [35])

    def test_find_peaks_dips(self):
        y = np.zeros(101)
        y[20] = 10
        y[50] = -10
        y[80] = 8
        peaks, dips = find_peaks_dips(y, window=2, unique_window=5)
        self.assertIn(20, [i for i, _ in peaks])
        self.assertIn(50, [i for i, _ in dips])


if __name__ == "__main__":
    unittest.main()
