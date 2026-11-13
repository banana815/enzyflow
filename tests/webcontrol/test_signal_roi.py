import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from webcontrol.signal_roi import detect_signal_roi,SignalRoiFinder
from webcontrol.spectrometer import Spectrometer
from spectrometer.spectro.camera import SyntheticSource

class SignalRoiTests(unittest.TestCase):
    def test_all_disconnected_weak_colours_enclosed_without_hot_pixel(self):
        f=np.zeros((100,200,3),np.uint8)
        f[40:46,20:28,0]=8  # faint blue, far below 10% of green peak
        f[40:46,80:90,1]=240
        f[42:48,170:178,2]=12
        f[2,199,:]=255
        r=detect_signal_roi(f)
        self.assertTrue(r.contains(20,40));self.assertTrue(r.contains(177,47))
        self.assertFalse(r.contains(199,2))
    def test_dark_noise_and_hot_pixels_do_not_create_roi(self):
        rng=np.random.default_rng(9)
        f=np.clip(rng.normal(8,1,(100,200,3)),0,255).astype(np.uint8)
        f[2,199]=255
        self.assertIsNone(detect_signal_roi(f))
    def test_signal_at_image_edge_is_bounded(self):
        f=np.zeros((100,200,3),np.uint8);f[0:8,0:8]=20;f[90:100,190:200]=30
        r=detect_signal_roi(f)
        self.assertEqual(r.as_tuple(),(0,0,200,100))
    def test_temporal_average_rejects_single_frame_flash(self):
        finder=SignalRoiFinder()
        for i in range(8):
            f=np.zeros((100,200,3),np.uint8);f[40:46,60:90]=40
            if i==0:f[3:6,3:6]=10
            r=finder.add(f)
            if i<7:self.assertIsNone(r)
        self.assertFalse(r.contains(4,4));self.assertTrue(r.contains(70,42))

class AutoRoiWorkerTests(unittest.TestCase):
    def wait(self,predicate):
        end=time.monotonic()+10
        while time.monotonic()<end:
            s=self.service.snapshot()
            if predicate(s):return s
            time.sleep(.03)
        self.fail(str(self.service.snapshot().get('error')))
    def test_dark_waits_for_signal_then_geometry_persists_across_dark_and_restart(self):
        lit=[False]
        def frame(source):
            f=np.zeros((720,1280,3),np.uint8)
            if lit[0]:f[390:412,100:1100]=30
            return f
        with tempfile.TemporaryDirectory() as d,patch.object(SyntheticSource,'read',frame):
            self.service=Spectrometer(demo=True,data_dir=d)
            try:
                self.service.command({'op':'connect'})
                self.wait(lambda s:s['connected'])
                self.service.command({'op':'dark_start','confirmed':True,'frames':8})
                s=self.wait(lambda s:bool(s['error']))
                self.assertIn('识别',s['error'])
                self.assertFalse((Path(d)/'demo-instrument-profile.json').exists())
                lit[0]=True
                s=self.wait(lambda s:s.get('roi_selection',{}).get('ready'))
                bounds=s['record']['roi']
                self.assertLessEqual(bounds['x'],100)
                self.assertGreaterEqual(bounds['x']+bounds['w'],1100)
                lit[0]=False
                self.service.command({'op':'dark_start','confirmed':True,'frames':8})
                s=self.wait(lambda s:s['dark']['status']=='ready')
                self.assertEqual(s['record']['roi'],bounds)
                dark_id=s['dark']['metadata']['id']
                # Searching in darkness must not replace the saved geometry/dark.
                self.service.command({'op':'auto_roi'})
                self.wait(lambda s:s.get('roi_selection',{}).get('scanning'))
                self.service.close()
                self.service=Spectrometer(demo=True,data_dir=d)
                self.service.command({'op':'connect'})
                s=self.wait(lambda s:s['connected'])
                self.assertEqual(s['record']['roi'],bounds)
                self.assertEqual(s['dark']['metadata']['id'],dark_id)
            finally:self.service.close()
