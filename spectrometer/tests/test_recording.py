import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from spectro.calibration import Calibration
from spectro.recording import append_jsonl, make_record, save_capture, save_csv
from spectro.roi import Roi
from spectro.spectrum import SpectrumResult


def _result() -> SpectrumResult:
    n = 16
    base = np.linspace(10.0, 30.0, n, dtype=np.float32)
    return SpectrumResult(
        pixels=np.arange(100, 100 + n),
        r=base + 30,
        g=base,
        b=base - 5,
        luma=base,
        intensity=base,
        display=base,
        wavelengths=np.linspace(400, 700, n),
        saturated=False,
        saturated_ratio=0.0,
        raw_max=30.0,
        buffer_len=4,
        zero_active=False,
        hold_active=False,
    )


class RecordingTests(unittest.TestCase):
    def test_save_csv_and_capture(self):
        calibration = Calibration.fit([(100, 400), (110, 550), (115, 700)])
        with tempfile.TemporaryDirectory() as tmp:
            result = _result()
            csv_path = Path(tmp) / "data.csv"
            save_csv(csv_path, result)
            with csv_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 16)
            self.assertEqual(rows[0]["pixel"], "100")
            self.assertTrue(rows[0]["wavelength_nm"])

            image = np.zeros((40, 60, 3), dtype=np.uint8)
            files = save_capture(
                tmp,
                result,
                calibration=calibration,
                roi=Roi(100, 0, 16, 4),
                source_info={"kind": "test", "path": "/dev/video-test"},
                exposure={"auto": False, "value": 300},
                graph_image=image,
                save_ui=False,
                save_frame=False,
                stem="unit",
            )
            self.assertTrue(Path(files["csv"]).exists())
            self.assertTrue(Path(files["json"]).exists())
            self.assertTrue(Path(files["graph_png"]).exists())
            metadata = json.loads(Path(files["json"]).read_text())
            self.assertEqual(metadata["schema"], 1)
            self.assertEqual(metadata["roi"]["w"], 16)
            self.assertEqual(metadata["calibration"]["degree"], 2)

    def test_record_is_json_serialisable(self):
        result = _result()
        record = make_record(
            result,
            calibration=None,
            roi=Roi(0, 0, 16, 1),
            source_info={"path": "synthetic"},
            exposure={"auto": True, "value": None},
        )
        text = json.dumps(record)
        self.assertIn('"source": "pc.usb_spectrometer"', text)
        self.assertEqual(len(record["wavelength_nm"]), 16)

    def test_jsonl_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.jsonl"
            append_jsonl(path, {"a": 1})
            append_jsonl(path, {"a": 2})
            lines = path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["a"], 2)


if __name__ == "__main__":
    unittest.main()
