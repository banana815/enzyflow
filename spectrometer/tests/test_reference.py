import tempfile
import unittest
from pathlib import Path

import numpy as np

from spectro.reference import (
    apply_scaling,
    interpolate_reference,
    load_reference_csv,
    scaling_factors,
)
from spectro.tungsten import save_tungsten_csv, tungsten_reference


class TungstenTests(unittest.TestCase):
    def test_reference_is_normalised(self):
        wavelengths, values = tungsten_reference(2800, 340, 1200)
        self.assertEqual(wavelengths[0], 340)
        self.assertTrue(np.all(np.diff(wavelengths) > 0))
        self.assertAlmostEqual(float(np.max(values)), 1.0, places=6)
        self.assertTrue(np.all(values >= 0))

    def test_save_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tungsten.csv"
            save_tungsten_csv(path, 2800, 340, 800)
            self.assertTrue(path.exists())
            wavelengths, values = load_reference_csv(path)
        self.assertEqual(len(wavelengths), len(values))
        self.assertEqual(wavelengths[0], 340)


class ReferenceTests(unittest.TestCase):
    def test_interpolate(self):
        ref_w = np.array([400.0, 500.0, 600.0])
        ref_v = np.array([0.0, 1.0, 0.0])
        measured = np.array([450.0, 550.0, 650.0])
        result = interpolate_reference(ref_w, ref_v, measured)
        np.testing.assert_allclose(result, [0.5, 0.5, 0.0])

    def test_scaling_and_apply(self):
        wavelengths = np.array([400.0, 500.0, 600.0])
        measured = np.array([10.0, 20.0, 30.0])
        ref_w = np.array([400.0, 500.0, 600.0])
        ref_v = np.array([20.0, 20.0, 60.0])
        factors = scaling_factors(wavelengths, measured, ref_w, ref_v)
        np.testing.assert_allclose(factors, [2.0, 1.0, 2.0])
        np.testing.assert_allclose(apply_scaling(measured, factors), [20.0, 20.0, 60.0])


if __name__ == "__main__":
    unittest.main()
