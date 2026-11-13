import unittest
import test_controller as fixtures
from webcontrol.controller import APIError
from webcontrol.transport import ConnectionFailure

class ParallelControllerTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    post = fixtures.ControllerTests.post
    ready = fixtures.ControllerTests.ready
    run_job = fixtures.ControllerTests.run_job
    jogs = fixtures.ControllerTests.jogs
    def ready_all(self):
        self.ready()
        for i in (2,3):
            self.post('anchor',axis=i,mm=100,sign=-1,lead=4,confirmed=True); self.c.step()

    def group(self):
        return [{'axis':1,'target_mm':53,'rpm':12}, {'axis':2,'target_mm':99,'rpm':6}, {'axis':3,'target_mm':103,'rpm':12}]

    def test_group_completes_three_targets_and_duplicate_is_not_replayed(self):
        self.ready_all()
        body={'client_id':'page-a','request_id':'group-once','moves':self.group()}
        self.c.submit('move_group',body);self.run_job()
        job=self.c.snapshot()['job']
        self.assertEqual(job['state'],'done')
        self.assertEqual([a['position_mm'] for a in self.c.snapshot()['axes']],[53,99,103])
        count=len(self.transport.calls)
        self.assertTrue(self.c.submit('move_group',body)['duplicate'])
        self.assertEqual(len(self.transport.calls),count)

    def test_group_invalid_member_is_atomic(self):
        self.ready_all();moves=self.group();moves[-1]['target_mm']=500
        with self.assertRaises(APIError): self.post('move_group',moves=moves)
        self.assertIsNone(self.c.snapshot()['job'])
        self.assertEqual(self.jogs(),[])

    def test_cancel_stops_all_group_members_after_current_segment(self):
        self.ready_all();self.post('move_group',moves=self.group());self.c.step()
        count=len(self.jogs());self.post('stop');self.c.step();self.c.step()
        self.assertEqual(len(self.jogs()),count)
        self.assertEqual(self.c.snapshot()['job']['state'],'cancelled')
        self.assertTrue(all(a['anchored'] for a in self.c.snapshot()['axes']))

    def test_group_ambiguous_failure_is_not_replayed(self):
        self.ready_all();self.post('move_group',moves=self.group())
        original=self.transport.request
        def fail(op,args=None):
            result=original(op,args)
            if op=='move_continuous': raise ConnectionFailure('unknown result')
            return result
        self.transport.request=fail
        self.c.step();count=len(self.jogs());self.clock.now+=3;self.c.step();self.c.step()
        self.assertEqual(len(self.jogs()),count)
        self.assertEqual(self.c.snapshot()['job']['state'],'failed')

    def test_recover_requires_confirmation_and_inward_direction(self):
        self.ready(position=20)
        for args in ({'confirmed':False,'delta_mm':.5,'rpm':6},{'confirmed':True,'delta_mm':-.5,'rpm':6},{'confirmed':True,'delta_mm':.5,'rpm':12}):
            with self.assertRaises(APIError): self.post('recover',axis=1,**args)
        self.post('recover',axis=1,delta_mm=.5,rpm=6,confirmed=True);self.run_job()
        self.assertEqual(self.c.snapshot()['axes'][0]['position_mm'],20.5)
        self.assertEqual(self.c.snapshot()['job']['state'],'done')
        with self.assertRaises(APIError): self.post('jog',axis=1,delta_mm=.5,rpm=6)
