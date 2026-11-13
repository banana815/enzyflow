"""AS7341 processing regressions; no sensor or UART imports."""
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'firmware/lib/enzyflow/processing.py'
SPEC = importlib.util.spec_from_file_location('processing', SOURCE)
processing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(processing)


class ProcessingTests(unittest.TestCase):
    def test_debounce(self):
        detector = processing.BlockDetector(100)
        self.assertEqual(
            [detector.update(v) for v in (19, 21, 19, 19, 19)],
            [False, False, False, False, True],
        )

    def test_hysteresis(self):
        detector = processing.BlockDetector(100)
        for _ in range(3):
            detector.update(19)
        self.assertEqual(
            [detector.update(v) for v in (25, 34, 36, 34, 36, 36, 36)],
            [True, True, True, True, True, True, False],
        )

    def test_dark_neutral_and_red(self):
        for channels, expected in (
            ((0,) * 8, (0, 0, 0)),
            ((10,) * 8, (255, 255, 255)),
            ((0, 0, 0, 0, 0, 10, 10, 10), (255, 0, 0)),
        ):
            with self.subTest(channels=channels):
                self.assertEqual(processing.estimate_rgb(channels), expected)


if __name__ == '__main__':
    unittest.main()
