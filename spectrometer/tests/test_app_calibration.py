import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectro.app import SpectrometerApp
from spectro.camera import SyntheticSource
from spectro.config import AppConfig


class AppCalibrationTests(unittest.TestCase):
    def test_gui_calibration_prompts_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = AppConfig()
            config.normalise()
            source = SyntheticSource(1280, 720)
            app = SpectrometerApp(config, data_dir / "config.json", data_dir, source, None)
            app.cal_points = [90, 580, 800]
            with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
                "builtins.input", side_effect=["405.4", "546.5", "611.6"]
            ):
                app._calibrate()
            self.assertIsNotNone(app.calibration)
            self.assertEqual(app.cal_points, [])
            saved = data_dir / "calibration.json"
            self.assertTrue(saved.exists())
            self.assertEqual(len(app.calibration.points), 3)


if __name__ == "__main__":
    unittest.main()
