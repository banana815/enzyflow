import copy
import math
from pathlib import Path
import tempfile
import unittest
from webcontrol.halogen_reference import capture, compare
from webcontrol.spectrometer import Spectrometer

class HalogenTests(unittest.TestCase):
    def setUp(self):
        self.binding={'by_id':'only-camera'}
        self.settings={'roi':{'x':0,'w':3},'controls':{'exposure':100},'processing':dict(linearize='off',gain_r=1,gain_g=1,gain_b=1)}
        self.record={'stats':dict(dark_corrected=True,zero_reference=False,saturated=False,buffer_len=32),
                     'pixel':[0,1,2],'intensity':[100,100,0.5],'dark_correction':{'metadata':{'id':'dark','temporal_std_mean':1}}}
    def test_transmission_absorbance_and_weak_reference(self):
        ref=capture(self.record,self.binding,self.settings,True)
        sample=copy.deepcopy(self.record);sample['intensity']=[50,125,0.4]
        value=compare(ref,sample,self.binding,self.settings)
        self.assertEqual(value['transmittance'],[.5,1.25,None])
        self.assertAlmostEqual(value['absorbance'][0],math.log10(2))
        self.assertLess(value['absorbance'][1],0)
    def test_condition_mismatch_and_new_dark_disable_ratio(self):
        ref=capture(self.record,self.binding,self.settings,True)
        for key in ('roi','controls','processing'):
            changed=copy.deepcopy(self.settings)
            if key=='processing':changed[key]['gain_r']=2
            else:changed[key]['x']=1
            self.assertFalse(compare(ref,self.record,self.binding,changed)['active'])
        sample=copy.deepcopy(self.record);sample['dark_correction']['metadata']['id']='new'
        self.assertFalse(compare(ref,sample,self.binding,self.settings)['active'])
    def test_reject_unconfirmed_unaveraged_or_saturated_blank(self):
        with self.assertRaises(ValueError):capture(self.record,self.binding,self.settings,False)
        for key,value in [('buffer_len',8),('dark_corrected',False),('zero_reference',True),('saturated',True)]:
            rec=copy.deepcopy(self.record);rec['stats'][key]=value
            with self.assertRaises(ValueError):capture(rec,self.binding,self.settings,True)
    def test_historical_files_survive_service_restart_and_exclude_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'captures';path.mkdir()
            (path/'series-old.jsonl').write_text('{}\n')
            (path/'secret.json').symlink_to('/etc/passwd')
            service=Spectrometer(data_dir=d)
            self.assertIn('series-old.jsonl',service.exports)
            self.assertNotIn('secret.json',service.exports)
            self.assertEqual(service.snapshot()['archive'][0]['url'],'/api/spectrometer-file/series-old.jsonl')
