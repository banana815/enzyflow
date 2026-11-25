import copy
import json
from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from webcontrol.instrument_profile import read_profile,write_profile,dark_matches,restore_controls
from webcontrol.calibration_history import CalibrationHistory
from spectrometer.spectro.config import ProcessingConfig

class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.binding={'width':16,'height':8,'vid_pid':'test','by_id':'one','name':'camera'}
        self.settings={'roi':{'x':0,'y':2,'w':16,'h':2},'controls':{'auto_exposure':1,'exposure_time_absolute':100},'processing':vars(ProcessingConfig())}
        self.data={'schema':1,'binding':self.binding,'settings':self.settings,'dark':{
            'background':[10.]*16,'metadata':dict(self.settings,binding=self.binding,frames=32,id='dark-id')}}
    def test_atomic_roundtrip_and_binding_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'instrument-profile.json'
            write_profile(path,self.data)
            self.assertEqual(read_profile(path,self.binding),self.data)
            with self.assertRaises(ValueError):read_profile(path,dict(self.binding,width=32))
            changed=copy.deepcopy(self.data);changed['dark']=None
            write_profile(path,changed)
            self.assertEqual(json.loads(path.with_suffix('.previous.json').read_text()),self.data)
            self.assertIsNone(read_profile(path,self.binding)['dark'])
    def test_corrupt_profile_does_not_get_used(self):
        for change in ('nan','width','roi'):
            bad=copy.deepcopy(self.data)
            if change=='nan':bad['dark']['background'][0]=float('nan')
            elif change=='width':bad['dark']['background'].pop()
            else:bad['settings']['roi']['x']=100
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'profile.json';path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):read_profile(path,self.binding)

    def test_shared_history_can_be_loaded_without_runtime_and_edits_stay_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            runtime = root / "runtime"
            saved = CalibrationHistory(shared, self.binding)
            key, filename = saved.save(self.data)
            write_profile(shared / "instrument-profile.json", self.data)

            history = CalibrationHistory(runtime, self.binding, archive_dir=shared)
            self.assertEqual([entry["id"] for entry in history.scan()], [key, "legacy"])
            self.assertEqual(history.read(key), self.data)
            self.assertFalse(runtime.exists())

            updated = dict(self.data, dark=None)
            history.save(updated, key)
            self.assertTrue((runtime / "calibrations" / filename).is_file())
            self.assertEqual(history.read(key), updated)
            self.assertEqual(saved.read(key), self.data)
            wrong_device = CalibrationHistory(runtime, dict(self.binding, by_id="other"), archive_dir=shared)
            self.assertEqual(wrong_device.scan(), [])
    def test_display_changes_preserve_dark_exposure_roi_gain_do_not(self):
        s=self.settings
        p=dict(s['processing'],buffer_size=32,savgol_enabled=False,hold_peaks=True)
        self.assertTrue(dark_matches(self.data['dark'],self.binding,s['roi'],p,s['controls']))
        self.assertFalse(dark_matches(self.data['dark'],self.binding,s['roi'],dict(p,gain_r=2),s['controls']))
        self.assertFalse(dark_matches(self.data['dark'],self.binding,dict(s['roi'],y=3),p,s['controls']))
        self.assertFalse(dark_matches(self.data['dark'],self.binding,s['roi'],p,dict(s['controls'],exposure_time_absolute=200)))
    def test_restore_controls_uses_manual_before_exposure_and_checks_bounds(self):
        class Fake:
            values={'auto_exposure':3,'exposure_time_absolute':50}
            writes=[]
            def controls(self):return {k:{'value':v,'min':1,'max':5000} for k,v in self.values.items()}
            def set_control(self,k,v):self.values[k]=v;self.writes.append((k,v))
        source=Fake()
        restore_controls(source,self.settings['controls'])
        self.assertEqual(source.writes,[('auto_exposure',1),('exposure_time_absolute',100)])
        with self.assertRaises(ValueError):restore_controls(source,{'exposure_time_absolute':6000,'auto_exposure':1})
