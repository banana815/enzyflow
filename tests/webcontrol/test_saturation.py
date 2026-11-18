import copy
import tempfile
from pathlib import Path
import unittest
import numpy as np
from webcontrol import saturation
from webcontrol.halogen_reference import capture, compare

class SaturationTests(unittest.TestCase):
    def test_threshold_boundary_and_gate(self):
        image=np.full((10,10,3),250,np.uint8)
        record={'stats':{}}
        saturation.annotate(record,image,{'block':False,'threshold':250})
        self.assertTrue(record['stats']['saturated'])
        self.assertFalse(saturation.blocked(record))
        saturation.annotate(record,image,{'block':True,'threshold':251})
        self.assertFalse(record['stats']['saturated'])
        saturation.annotate(record,image,{'block':True,'threshold':250})
        self.assertTrue(saturation.blocked(record))

    def test_blank_and_sample_allowed_when_disabled(self):
        from tests.webcontrol.test_halogen_reference import HalogenTests
        fixture=HalogenTests();fixture.setUp()
        record=copy.deepcopy(fixture.record)
        saturation.annotate(record,np.full((3,3,3),255,np.uint8),{'block':False,'threshold':250})
        ref=capture(record,fixture.binding,fixture.settings,True)
        result=compare(ref,record,fixture.binding,fixture.settings)
        self.assertTrue(result['active'])
        self.assertTrue(result['saturation_warning'])
        record['saturation']['block']=True
        with self.assertRaises(ValueError):capture(record,fixture.binding,fixture.settings,True)
        self.assertFalse(compare(ref,record,fixture.binding,fixture.settings)['active'])

    def test_persistence_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            self.assertFalse(saturation.load(path)['block'])
            policy={'block':True,'threshold':255}
            saturation.save(path,policy)
            self.assertEqual(saturation.load(path),policy)
            for threshold in (0,256,True,250.5,'250'):
                with self.assertRaises(ValueError):saturation.save(path,dict(policy,threshold=threshold))
            self.assertEqual(saturation.load(path),policy)
