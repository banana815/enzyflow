"""USB separation and camera lifecycle; no real devices are opened."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from webcontrol.spectrometer import Spectrometer, eligible, identity, select_camera, windows_camera


def device(path='/dev/video2', **kwargs):
    values=dict(path=path,capture=True,vid_pid='0c68:6464',driver='uvcvideo',by_id='sonix-index0',name='USB-ZH')
    values.update(kwargs)
    return SimpleNamespace(**values)


class IdentityTests(unittest.TestCase):
    def test_reject_serial_builtin_metadata_and_unknown(self):
        for d in [device('/dev/ttyUSB0'),device(vid_pid='5986:114e'),device(capture=False),device(driver='other')]:
            self.assertFalse(eligible(d))
            with self.assertRaises(ValueError):select_camera([d],identity(d))

    def test_renumber_uses_identity_and_ambiguity_fails(self):
        driver = 'directshow' if windows_camera else 'uvcvideo'
        original = device(driver=driver)
        renamed = device('/dev/video6', driver=driver)
        self.assertEqual(select_camera([renamed],identity(original)).path,'/dev/video6')
        for devices in ([original,renamed],[device(driver=driver,by_id='different')],[]):
            with self.assertRaises(ValueError):select_camera(devices,identity(original))


class CameraWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Spectrometer(demo=True,data_dir=self.tmp.name)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def wait(self,predicate):
        end=time.monotonic()+7
        while time.monotonic()<end:
            s=self.service.snapshot()
            if predicate(s):return s
            time.sleep(.03)
        self.fail(str(self.service.snapshot()))

    def start(self):
        self.service.command({'op':'connect'})
        state = self.wait(lambda s:s['connected'])
        # Extraction fixtures use a known band, including synthetic uniform dark
        # frames which intentionally contain no detectable light envelope.
        if not state.get('roi_selection',{}).get('ready'):
            state = self.apply({'op':'roi','x':0,'y':355,'w':1280,'h':8},lambda s:s['roi_selection']['ready'] and not s['roi_selection']['scanning'])
        return state

    def apply(self,command,predicate):
        seq=self.service.snapshot()['record']['seq']
        self.service.command(command)
        return self.wait(lambda s:s['record']['seq']>seq and predicate(s))

    def test_preview_reference_roi_calibration_and_export(self):
        s=self.start()
        self.assertIsNone(s['record']['wavelength_nm'])
        self.assertTrue(self.service.preview.startswith(b'\xff\xd8'))
        with self.assertRaises(ValueError):self.service.command({'op':'connect'})
        self.apply({'op':'zero'},lambda s:s['record']['stats']['zero_reference'])
        self.apply({'op':'roi','x':100,'y':355,'w':700,'h':8},lambda s:s['record']['roi']['x']==100)
        self.assertFalse(self.service.snapshot()['record']['stats']['zero_reference'])
        self.apply({'op':'calibrate','points':[[0,400],[640,550],[1279,700]]},lambda s:s['record']['calibration'] is not None)
        self.apply({'op':'save'},lambda s:bool(s['files']))
        files=[p for p in self.service.exports.values() if p.parent.name=='captures']
        self.assertEqual(len(files),4)
        data=json.loads(next(p for p in files if p.suffix=='.json').read_text())
        self.assertEqual(data['source'],'pc.usb_spectrometer')
        self.assertEqual(data['roi']['x'],100)
        self.assertTrue(data['calibration'])
        self.service.command({'op':'disconnect'})
        self.wait(lambda s:not s['connected'])

    def test_time_series_stops_and_can_be_exported(self):
        self.start()
        self.apply({'op':'record_start','interval':.2},lambda s:s.get('recording'))
        path=next(iter(self.service.exports.values()))
        self.wait(lambda s:path.stat().st_size>0)
        self.apply({'op':'record_stop'},lambda s:not s.get('recording'))
        count=len(path.read_text().splitlines())
        time.sleep(.3)
        self.assertEqual(len(path.read_text().splitlines()),count)
        self.assertEqual(json.loads(path.read_text().splitlines()[0])['source'],'pc.usb_spectrometer')

    def test_dark_collects_new_frames_and_invalidates_on_roi_change(self):
        import numpy as np
        from spectrometer.spectro.camera import SyntheticSource
        level=[10]
        def frame(source):
            return np.full((720,1280,3),level[0],dtype=np.uint8)
        with patch.object(SyntheticSource,'read',frame):
            self.start()
            self.apply({'op':'dark_start','frames':8,'confirmed':False},lambda s:bool(s['error']))
            self.assertEqual(self.service.snapshot()['dark']['status'],'none')
            self.apply({'op':'dark_start','frames':8,'confirmed':True},lambda s:s['dark']['status']=='collecting')
            s=self.wait(lambda s:s['dark']['status']=='ready')
            self.assertEqual(s['dark']['metadata']['frames'],8)
            self.assertAlmostEqual(s['dark']['metadata']['mean_counts'],10)
            self.assertAlmostEqual(s['record']['stats']['intensity_max'],0)
            level[0]=14
            self.wait(lambda s:abs(s['record']['stats']['intensity_max']-4)<1e-5)
            self.apply({'op':'save'},lambda s:bool(s['files']))
            metadata=json.loads(next(p for p in self.service.exports.values() if p.suffix=='.json').read_text())
            self.assertTrue(metadata['dark_correction']['active'])
            self.assertEqual(metadata['dark_correction']['metadata']['frames'],8)
            self.apply({'op':'roi','x':0,'y':0,'w':100,'h':4},lambda s:s['dark']['status']=='none')
            self.assertFalse(self.service.snapshot()['record']['stats']['dark_corrected'])

    def test_dark_average_ignores_old_buffer_and_uses_distinct_frames(self):
        import numpy as np
        from spectrometer.spectro.camera import SyntheticSource
        levels=[100]
        counter=[0]
        def frame(source):
            counter[0]+=1
            value=levels[counter[0]%len(levels)]
            return np.full((720,1280,3),value,dtype=np.uint8)
        with patch.object(SyntheticSource,'read',frame):
            self.start()
            levels[:]=[10,14]
            self.apply({'op':'dark_start','frames':8,'confirmed':True},lambda s:s['dark']['status']=='collecting')
            s=self.wait(lambda s:s['dark']['status']=='ready')
            self.assertAlmostEqual(s['dark']['metadata']['mean_counts'],12)
            self.assertAlmostEqual(s['dark']['metadata']['temporal_std_mean'],2)

    def test_real_mode_requires_manual_exposure_and_detects_external_change(self):
        import numpy as np
        from spectrometer.spectro import camera
        from spectrometer.spectro.camera import SyntheticSource, DeviceInfo
        class FakeCamera(SyntheticSource):
            auto=3
            def controls(self):
                return {'auto_exposure':{'value':self.auto},'exposure_time_absolute':{'value':100}}
            def get_exposure(self):return 100
            def set_exposure(self,value):self.auto=1
            def read(self):
                if windows_camera:
                    time.sleep(.03)
                return np.full((720,1280,3),10,dtype=np.uint8)
        fake=FakeCamera()
        self.service.demo=False
        with ExitStack() as stack:
            if windows_camera:
                d=DeviceInfo(path='test-camera',name='USB-ZH',vid_pid='0c68:6464',driver='directshow',by_id='test-camera')
                stack.enter_context(patch.object(windows_camera,'lock_camera',return_value=Mock()))
            else:
                d=DeviceInfo(path='/dev/video2',name='USB-ZH',vid_pid='0c68:6464',driver='uvcvideo')
                stack.enter_context(patch('webcontrol.spectrometer.fcntl.flock'))
            backend=windows_camera or camera
            stack.enter_context(patch.object(backend,'enumerate_devices',return_value=[d]))
            stack.enter_context(patch.object(backend,'CameraSource',return_value=fake))
            self.service.command({'op':'connect','device':identity(d)})
            self.wait(lambda s:s['connected'])
            self.assertEqual(fake.auto,1)
            self.apply({'op':'roi','x':0,'y':355,'w':1280,'h':8},lambda s:s['roi_selection']['ready'])
            self.apply({'op':'exposure','auto':True,'value':100},lambda s:bool(s['error']))
            self.assertIn('手动曝光',self.service.snapshot()['error'])
            self.apply({'op':'dark_start','frames':8,'confirmed':True},lambda s:s['dark']['status']=='collecting')
            self.wait(lambda s:s['dark']['status']=='ready')
            fake.auto=3
            self.wait(lambda s:s['dark']['status']=='none' and not s['record']['stats']['dark_corrected'])
            self.assertIn('相机参数发生变化',self.service.snapshot()['dark']['message'])
            self.service.close()

    def test_dark_cancel_and_settings_are_not_mixed(self):
        self.start()
        self.apply({'op':'dark_start','frames':64,'confirmed':True},lambda s:s['dark']['status']=='collecting')
        self.apply({'op':'zero'},lambda s:bool(s['error']))
        self.assertFalse(self.service.snapshot()['record']['stats']['zero_reference'])
        self.apply({'op':'dark_clear'},lambda s:s['dark']['status']=='none')
        self.assertFalse(self.service.snapshot()['record']['stats']['dark_corrected'])

    def test_two_stage_lamp_flow_persists_provenance_and_rejects_stale_snapshot(self):
        import numpy as np
        from spectrometer.spectro.camera import SyntheticSource
        lit=[False]
        def frame(source):
            x=np.arange(1280)
            values=np.full(1280,10.)
            if lit[0]:
                for pos in [100,260,810,1130]:values+=120*np.exp(-.5*((x-pos)/2)**2)
            row=np.repeat(values.astype(np.uint8)[None,:,None],3,axis=2)
            return np.repeat(row,720,axis=0)
        with patch.object(SyntheticSource,'read',frame):
            self.start()
            self.apply({'op':'lamp_capture','confirmed':True},lambda s:bool(s['error']))
            self.assertIsNone(self.service.snapshot()['lamp'])
            self.apply({'op':'dark_start','frames':8,'confirmed':True},lambda s:s['dark']['status']=='collecting')
            self.wait(lambda s:s['dark']['status']=='ready')
            lit[0]=True
            self.apply({'op':'lamp_capture','confirmed':True},lambda s:bool(s['error']))
            self.assertIn('32',self.service.snapshot()['error'])
            self.apply({'op':'processing','average':32,'smooth':True,'hold':False},lambda s:s['processing']['buffer_size']==32)
            self.wait(lambda s:s['record']['stats']['buffer_len']==32)
            self.wait(lambda s:s['record']['stats']['intensity_max']>100)
            self.apply({'op':'lamp_capture','confirmed':True},lambda s:s['lamp'] is not None)
            snap=self.service.snapshot()['lamp']
            self.apply({'op':'lamp_calibrate','snapshot_id':snap['id'],
                        'points':[[260,436],[810,546],[1130,610]],'check_points':[[100,404]]},
                       lambda s:s['record']['calibration'] is not None)
            s=self.service.snapshot()
            self.assertEqual(len(s['calibration_report']['checks']),1)
            self.assertEqual(s['record']['calibration']['verification']['dark_id'],s['dark']['metadata']['id'])
            saved=json.loads((Path(self.tmp.name)/'demo-calibration.json').read_text())
            self.assertEqual(saved['report']['snapshot_id'],snap['id'])
            self.assertEqual(saved['lamp']['intensity'],snap['intensity'])
            self.service.close()
            self.service=Spectrometer(demo=True,data_dir=self.tmp.name)
            self.assertEqual(self.service.snapshot()['saved_lamp']['id'],snap['id'])
            restored=self.start()
            self.assertEqual(restored['calibration_report']['snapshot_id'],snap['id'])
            self.assertIsNotNone(restored['record']['wavelength_nm'])
            self.apply({'op':'dark_clear'},lambda s:s['lamp'] is None)
            self.apply({'op':'lamp_calibrate','snapshot_id':snap['id'],
                        'points':[[260,436],[810,546],[1130,610]]},lambda s:bool(s['error']))
            self.assertIn('失效',self.service.snapshot()['error'])

    def test_can_save_lamp_calibration_before_dark(self):
        import numpy as np
        from spectrometer.spectro.camera import SyntheticSource

        def lamp_frame(source):
            x = np.arange(source.actual_width)
            values = np.full(source.actual_width, 10.)
            for pixel in (260, 810, 1130):
                values += 120 * np.exp(-.5 * ((x - pixel) / 4) ** 2)
            return np.broadcast_to(values.astype(np.uint8)[None, :, None],
                                   (source.actual_height, source.actual_width, 3)).copy()

        with patch.object(SyntheticSource,'read',lamp_frame):
            self.start()
            self.apply({'op':'roi','x':0,'y':354,'w':1280,'h':12},
                       lambda s:s['record']['roi']['w']==1280)
            self.apply({'op':'processing','average':32,'smooth':True,'hold':False},
                       lambda s:s['processing']['buffer_size']==32)
            self.wait(lambda s:s['record']['stats']['buffer_len']==32)
            frozen = self.apply({'op':'lamp_capture','confirmed':True},lambda s:s['lamp'] is not None)
            self.assertIsNone(frozen['lamp']['dark_id'])
            saved = self.apply({'op':'peaks_save','snapshot_id':frozen['lamp']['id'],'degree':1,
                                'points':[[260,436],[810,546],[1130,610]]},
                               lambda s:s['record']['calibration'] is not None)
            self.assertTrue(Path(saved['wavelength_calibration']['path']).is_file())
            entry = next(item for item in saved['history'] if item['id']==saved['active_history'])
            self.assertTrue(entry['roi'] and entry['peaks'])
            self.assertFalse(entry['dark'])

    def test_saved_dark_and_settings_survive_new_service_without_recapture(self):
        import numpy as np
        from spectrometer.spectro.camera import SyntheticSource
        def frame(source):return np.full((720,1280,3),10,dtype=np.uint8)
        with patch.object(SyntheticSource,'read',frame):
            self.start()
            self.apply({'op':'roi','x':12,'y':22,'w':100,'h':4},lambda s:s['record']['roi']['x']==12)
            self.apply({'op':'dark_start','frames':8,'confirmed':True},lambda s:s['dark']['status']=='collecting')
            s=self.wait(lambda s:s['dark']['status']=='ready')
            dark_id=s['dark']['metadata']['id']
            self.assertTrue((Path(self.tmp.name)/'demo-instrument-profile.json').is_file())
            self.service.close()
            self.service=Spectrometer(demo=True,data_dir=self.tmp.name)
            s=self.start()
            self.assertEqual(s['dark']['metadata']['id'],dark_id)
            self.assertEqual(s['record']['roi'],{'x':12,'y':22,'w':100,'h':4})
            self.assertAlmostEqual(s['record']['stats']['intensity_max'],0)
            self.apply({'op':'processing','average':12,'smooth':False,'hold':True},lambda s:s['processing']['buffer_size']==12)
            self.assertEqual(self.service.snapshot()['dark']['metadata']['id'],dark_id)
            self.apply({'op':'roi','x':20,'y':22,'w':100,'h':4},lambda s:s['dark']['status']=='none')
            self.assertTrue(self.service.snapshot()['profile']['dark_saved'])
            self.apply({'op':'profile_restore'},lambda s:s['dark']['status']=='ready')
            self.assertEqual(self.service.snapshot()['record']['roi']['x'],12)
            self.apply({'op':'dark_clear'},lambda s:s['dark']['status']=='none')
            self.service.close()
            self.service=Spectrometer(demo=True,data_dir=self.tmp.name)
            s=self.start()
            self.assertFalse(s['record']['stats']['dark_corrected'])
            self.assertFalse(s['profile']['dark_saved'])

    def test_failed_recalibration_does_not_overwrite_saved_dark(self):
        import numpy as np
        from spectrometer.spectro.camera import SyntheticSource
        def frame(source):return np.full((720,1280,3),10,dtype=np.uint8)
        with patch.object(SyntheticSource,'read',frame):
            self.start()
            self.apply({'op':'dark_start','frames':8,'confirmed':True},lambda s:s['dark']['status']=='collecting')
            s=self.wait(lambda s:s['dark']['status']=='ready');dark_id=s['dark']['metadata']['id']
            self.apply({'op':'dark_start','frames':64,'confirmed':True},lambda s:s['dark']['status']=='collecting')
            self.service.close()
            self.service=Spectrometer(demo=True,data_dir=self.tmp.name)
            s=self.start()
            self.assertEqual(s['dark']['metadata']['id'],dark_id)

    def test_halogen_reference_restores_and_exports_ratios(self):
        import numpy as np
        from spectrometer.spectro.camera import SyntheticSource
        level=[10]
        def frame(source):return np.full((720,1280,3),level[0],dtype=np.uint8)
        with patch.object(SyntheticSource,'read',frame):
            self.start()
            self.apply({'op':'dark_start','frames':8,'confirmed':True},lambda s:s['dark']['status']=='collecting')
            self.wait(lambda s:s['dark']['status']=='ready')
            level[0]=180
            self.apply({'op':'processing','average':8,'smooth':False,'hold':False},lambda s:s['processing']['buffer_size']==8)
            self.wait(lambda s:s['record']['stats']['buffer_len']==8 and s['record']['stats']['intensity_max']>169.9)
            level[0]=110
            self.apply({'op':'halogen_capture','confirmed':True},lambda s:(s.get('halogen_capture') or {}).get('status')=='collecting')
            s=self.wait(lambda s:s.get('halogen',{}).get('saved'))
            ref=s['halogen']['id']
            self.assertEqual(s['halogen']['frames'],32)
            self.assertEqual(s['processing']['buffer_size'],8)
            reference=json.loads((Path(self.tmp.name)/'demo-halogen-reference.json').read_text())
            self.assertAlmostEqual(reference['record']['intensity'][0],100)
            self.assertEqual(reference['record']['stats']['buffer_len'],32)
            level[0]=60
            self.wait(lambda s:s['record']['photometry']['active'] and abs(s['record']['photometry']['transmittance'][0]-.5)<1e-5)
            self.apply({'op':'save'},lambda s:bool(s['files']))
            data=json.loads(next(p for p in self.service.exports.values() if p.parent.name=='captures' and p.suffix=='.json').read_text())
            self.assertAlmostEqual(data['photometry']['transmittance'][0],.5)
            self.apply({'op':'absorbance_save','name':'BTB_pH7.00'},lambda s:bool(s.get('absorbance_capture')))
            saved=self.service.snapshot()['absorbance_capture']
            self.assertEqual(saved['name'],'BTB_pH7.00')
            capture=json.loads((Path(self.tmp.name)/'captures/BTB_pH7.00.json').read_text(encoding='utf8'))
            self.assertAlmostEqual(capture['record']['photometry']['absorbance'][0],.30103,places=5)
            self.assertEqual(capture['reference']['id'],ref)
            self.assertEqual(capture['record']['seq'],saved['seq'])
            self.apply({'op':'saturation_settings','block':True,'threshold':50},lambda s:s['record']['stats']['saturated'] and not s['record']['photometry']['active'])
            self.apply({'op':'saturation_settings','block':False,'threshold':50},lambda s:s['record']['photometry']['active'])
            self.assertEqual(self.service.snapshot()['halogen']['id'],ref)
            self.apply({'op':'absorbance_save','name':'allowed_saturation'},lambda s:s.get('absorbance_capture',{}).get('name')=='allowed_saturation')
            exported=json.loads((Path(self.tmp.name)/'captures/allowed_saturation.json').read_text())
            self.assertTrue(exported['record']['stats']['saturated'])
            self.assertFalse(exported['record']['saturation']['block'])
            self.apply({'op':'saturation_settings','block':True,'threshold':200},lambda s:s['record']['photometry']['active'] and not s['record']['stats']['saturated'])


            self.service.close()
            self.service=Spectrometer(demo=True,data_dir=self.tmp.name)
            s=self.start()
            self.assertEqual(s['halogen']['id'],ref)
            self.assertTrue(s['record']['photometry']['active'])

    def test_blank_uses_loaded_dark_with_unsaved_range(self):
        import numpy as np
        from spectrometer.spectro.camera import SyntheticSource

        level = [10]
        def frame(source):
            return np.full((720,1280,3),level[0],dtype=np.uint8)

        with patch.object(SyntheticSource,'read',frame):
            self.start()
            self.apply({'op':'dark_start','frames':8,'full_frame':True,'confirmed':True},
                       lambda s:s['dark']['status']=='collecting')
            self.wait(lambda s:s['dark']['status']=='ready')
            level[0] = 110
            first = self.apply({'op':'halogen_capture','confirmed':True},
                               lambda s:s.get('halogen',{}).get('active'))
            self.assertTrue(first['dark']['savable'])
            initial_reference = first['halogen']['id']
            saved = self.apply({'op':'dark_save'},lambda s:s.get('active_history') is not None)
            key = saved['active_history']
            self.assertEqual(saved['halogen']['id'],initial_reference)
            archive = Path(self.tmp.name)/'demo-calibrations'/(key+'.json')
            original = archive.read_bytes()

            changed = {'x':100,'y':355,'w':700,'h':8}
            def set_draft():
                self.apply({'op':'roi','draft':True,**changed},lambda s:s['record']['roi']==changed)

            self.apply({'op':'history_load','id':key},lambda s:s.get('active_history')==key)
            set_draft()
            self.assertTrue(self.service.snapshot()['record']['stats']['dark_corrected'])
            updated = self.apply({'op':'halogen_capture','confirmed':True},
                                 lambda s:s.get('halogen',{}).get('active'))
            new_reference = updated['halogen']['id']
            self.assertNotEqual(new_reference,initial_reference)
            self.assertEqual(archive.read_bytes(),original)
            self.apply({'op':'history_load','id':key},lambda s:s['halogen'].get('id')==initial_reference)
            set_draft()
            self.assertEqual(self.service.snapshot()['halogen']['id'],new_reference)
            self.apply({'op':'absorbance_save','name':'draft_roi'},lambda s:s.get('absorbance_capture') is not None)
            exported = json.loads((Path(self.tmp.name)/'captures/draft_roi.json').read_text())
            self.assertEqual(exported['reference']['id'],new_reference)

    def test_invalid_settings_do_not_kill_capture(self):
        self.start()
        s=self.apply({'op':'roi','x':-1,'y':0,'w':10,'h':8},lambda s:bool(s['error']))
        self.assertTrue(s['connected'])
        self.assertNotEqual(s['record']['roi']['x'],-1)
        self.apply({'op':'processing','average':1,'smooth':False,'hold':False},lambda s:s['error'] is None)

    def test_camera_failure_does_not_reconnect(self):
        from spectrometer.spectro.camera import SyntheticSource
        with patch.object(SyntheticSource,'read',return_value=None) as read:
            self.service.command({'op':'connect'})
            self.wait(lambda s:s['status']=='error')
            self.assertEqual(read.call_count,1)
            self.assertFalse(self.service.snapshot()['connected'])

    def test_calibration_only_restores_matching_identity_and_resolution(self):
        self.start()
        self.apply({'op':'calibrate','points':[[0,400],[640,550],[1279,700]]},lambda s:s['record']['calibration'] is not None)
        self.service.close()
        self.service.command({'op':'connect'})
        self.wait(lambda s:s['connected'])
        self.assertIsNotNone(self.service.snapshot()['record']['calibration'])
        self.service.close()
        path=Path(self.tmp.name)/'demo-calibration.json'
        saved=json.loads(path.read_text());saved['binding']['width']=640;path.write_text(json.dumps(saved))
        self.service.command({'op':'connect'})
        self.wait(lambda s:s['connected'])
        self.assertIsNone(self.service.snapshot()['record']['calibration'])

if __name__=='__main__':unittest.main()
