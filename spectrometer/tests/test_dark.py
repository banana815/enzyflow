import csv
import json
import tempfile
from pathlib import Path
import unittest
import numpy as np
from spectro.config import ProcessingConfig
from spectro.spectrum import SpectrumProcessor
from spectro.recording import make_record, save_csv
from spectro.roi import Roi

class DarkTests(unittest.TestCase):
    def setUp(self):
        self.p=SpectrumProcessor(ProcessingConfig(buffer_size=1,savgol_enabled=False))
        self.profile=np.array([10,11,12,13],dtype=float)
        self.p.set_dark_reference(self.profile,{'frames':32,'id':'unit-dark'},100)
    def frame(self,values):
        return np.repeat(np.array(values,dtype=np.uint8)[None,:,None],3,axis=2)
    def test_pixelwise_subtraction_preserves_negative_raw_and_rgb(self):
        r=self.p.update(self.frame([9,15,12,20]),100)
        np.testing.assert_allclose(r.intensity,[-1,4,0,7])
        np.testing.assert_allclose(r.raw_intensity,[9,15,12,20])
        np.testing.assert_allclose(r.r,[9,15,12,20])
        self.assertTrue(r.summary()['dark_corrected'])
    def test_zero_is_after_dark_and_is_not_double_subtracted(self):
        self.p.update(self.frame([20,21,22,23]),100)
        self.p.set_zero_reference()
        r=self.p.update(self.frame([21,22,23,24]),100)
        np.testing.assert_allclose(r.intensity,[1,1,1,1])
        self.p.clear_zero_reference()
        np.testing.assert_allclose(self.p.update(self.frame([21,22,23,24]),100).intensity,[11]*4)
    def test_shift_reset_and_clear_invalidate_reference(self):
        r=self.p.update(self.frame([20]*4),101)
        self.assertFalse(r.summary()['dark_corrected'])
        self.p.set_dark_reference(self.profile,{},100)
        self.p.clear_dark_reference()
        np.testing.assert_allclose(self.p.update(self.frame([20]*4),100).intensity,[20]*4)
    def test_export_preserves_profile_and_signed_corrected_data(self):
        r=self.p.update(self.frame([9]*4),100)
        record=make_record(r,calibration=None,roi=Roi(100,0,4,1),source_info={},exposure={})
        self.assertEqual(record['dark_correction']['background'],[10,11,12,13])
        self.assertEqual(record['dark_correction']['metadata']['frames'],32)
        json.dumps(record,allow_nan=False)
        with tempfile.TemporaryDirectory() as tmp:
            path=save_csv(Path(tmp)/'dark.csv',r)
            with path.open() as f: row=next(csv.DictReader(f))
            self.assertEqual(float(row['raw_intensity']),9)
            self.assertEqual(float(row['dark_background']),10)
            self.assertEqual(float(row['intensity']),-1)
