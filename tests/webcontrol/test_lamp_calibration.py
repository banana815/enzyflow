import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from spectrometer.spectro.spectrum import SpectrumProcessor
from spectrometer.spectro.config import ProcessingConfig
from spectrometer.spectro.roi import Roi
from spectrometer.spectro.calibration import Calibration
from webcontrol.lamp_calibration import freeze_lamp,fit_lamp

class LampTests(unittest.TestCase):
    def result(self,reverse=False):
        pixels=np.arange(1280)
        values=np.full(1280,10.)
        for pos in [100,260,810,1130]:
            if reverse:pos=1279-pos
            values+=120*np.exp(-.5*((pixels-pos)/2)**2)
        frame=np.repeat(np.clip(values,0,255).astype(np.uint8)[None,:,None],3,axis=2)
        p=SpectrumProcessor(ProcessingConfig(buffer_size=1,savgol_enabled=False))
        p.set_dark_reference(np.full(1280,10),{'id':'dark-fixture'},0)
        return p,p.update(frame)
    def snapshot(self,reverse=False):
        p,r=self.result(reverse)
        return freeze_lamp(r,{'width':1280},Roi(0,0,1280,1),1)
    def test_fit_actual_pixels_and_independent_reference_check(self):
        snap=self.snapshot()
        cal,report=fit_lamp(snap,[[260,436],[810,546],[1130,610]],[[100,404]])
        self.assertAlmostEqual(cal.wavelength_at(100),404)
        self.assertEqual(report['dark_id'],'dark-fixture')
        self.assertEqual(report['pixel_range'],[260,1130])
        self.assertEqual(len(report['checks']),1)
        self.assertIn('整数',report['reference_precision'])
        self.assertEqual(Calibration.from_dict(cal.to_dict()).verification,report)
    def test_reversed_spectral_direction_is_supported(self):
        cal,_=fit_lamp(self.snapshot(True),[[1019,436],[469,546],[149,610]],[[1179,404]])
        self.assertAlmostEqual(cal.wavelength_at(1179),404)
    def test_duplicate_off_peak_unknown_and_wrong_order_rejected(self):
        for pairs in ([[260,436],[260,546],[1130,610]],
                      [[400,436],[810,546],[1130,610]],
                      [[260,435.8],[810,546],[1130,610]],
                      [[260,546],[810,436],[1130,610]]):
            with self.assertRaises(ValueError):fit_lamp(self.snapshot(),pairs)
    def test_no_dark_or_zero_reference_rejected(self):
        p,r=self.result()
        r.dark_profile=None
        with self.assertRaisesRegex(ValueError,'暗场'):freeze_lamp(r,{'width':1280},Roi(0,0,1280,1),1)
        p,r=self.result();r.zero_active=True
        with self.assertRaisesRegex(ValueError,'零参考'):freeze_lamp(r,{'width':1280},Roi(0,0,1280,1),1)
    def test_saturation_and_dark_only_rejected(self):
        _,r=self.result();r.saturated=True
        with self.assertRaisesRegex(ValueError,'饱和'):freeze_lamp(r,{'width':1280},Roi(0,0,1280,1),1)
        _,r=self.result();r.intensity[:]=0
        with self.assertRaisesRegex(ValueError,'过弱'):freeze_lamp(r,{'width':1280},Roi(0,0,1280,1),1)

    def test_linear_option_handles_quadratic_turning_without_hiding_residuals(self):
        snap=self.snapshot()
        pairs=[[260,436],[810,546],[1130,610]]
        # Deliberately uneven spacing: quadratic turns within the full frame.
        snap['peaks']=[{'pixel':p} for p in [600,650,900]]
        pairs=[[600,436],[650,546],[900,610]]
        with self.assertRaisesRegex(ValueError,'折返'):
            fit_lamp(snap,pairs,degree=2)
        cal,report=fit_lamp(snap,pairs,degree=1)
        self.assertTrue(np.all(np.diff(cal.wavelengths(0,1280))>0))
        self.assertGreater(report['rms_nm'],10)
        self.assertEqual(report['degree'],1)
        self.assertEqual(Calibration.from_dict(cal.to_dict()).degree,1)
        for invalid in [True,0,3,'1']:
            with self.assertRaises(ValueError):fit_lamp(snap,pairs,degree=invalid)
