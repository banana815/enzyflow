import json
from pathlib import Path
import tempfile
import unittest
import test_controller as fixtures
from webcontrol.controller import Controller, APIError
from webcontrol.demo import DemoTransport
from webcontrol.transport import SerialTransport, ConnectionFailure

class EmergencyResetTests(unittest.TestCase):
    setUp=fixtures.ControllerTests.setUp
    tearDown=fixtures.ControllerTests.tearDown
    post=fixtures.ControllerTests.post
    ready=fixtures.ControllerTests.ready
    def run_job(self):
        self.transport.clock = self.clock
        for _ in range(1000):
            self.post('heartbeat')
            self.c.step()
            if self.c.snapshot()['job']['state'] not in ('running', 'stopping'):
                return
            self.clock.now += 0.1
        self.fail('Continuous reset did not finish')
    def ready_all(self):
        self.ready()
        for i in (2,3):self.post('anchor',axis=i,mm=100,sign=-1,lead=4,confirmed=True);self.c.step()
    def test_viewer_can_estop_and_latch_blocks_moves_until_explicit_release(self):
        self.ready_all()
        self.c.submit('emergency_stop',dict(client_id='viewer'))
        self.assertTrue(self.c.snapshot()['estop_latched'])
        with self.assertRaises(APIError):self.post('jog',axis=1,delta_mm=.5,rpm=12)
        self.c.step()
        self.assertTrue(self.c.snapshot()['estop_confirmed'])
        self.assertFalse(self.c.snapshot()['connected'])
        with self.assertRaises(APIError):self.post('clear_estop',confirmed=False)
        self.post('clear_estop',confirmed=True)
        self.assertTrue(self.c.snapshot()['estop_latched'])
        self.c.step()
        self.assertFalse(self.c.snapshot()['estop_latched'])
        self.assertTrue(self.c.snapshot()['connected'])
        self.assertTrue(all(not a['anchored'] for a in self.c.snapshot()['axes']))
    def test_recovery_failure_keeps_latch_and_never_replays_job(self):
        self.ready_all();self.post('emergency_stop');self.c.step()
        original=self.transport.request
        def fail(op,args=None):
            if op=='restore_drives':raise ConnectionFailure('driver fault')
            return original(op,args)
        self.transport.request=fail
        self.post('clear_estop',confirmed=True);self.c.step()
        self.assertTrue(self.c.snapshot()['estop_latched'])
        self.assertFalse(self.c.snapshot()['connected'])
        self.assertIn('driver fault',self.c.snapshot()['last_error'])

    def test_emergency_signal_occurs_inside_inflight_jog_not_next_worker_step(self):
        self.ready_all();signals=[]
        original=self.transport.emergency_stop
        def emergency():signals.append('now');original()
        self.transport.emergency_stop=emergency
        def mid_segment():
            self.post('emergency_stop')
            self.assertEqual(signals,['now'])
        self.transport.after_jog=mid_segment
        self.post('jog',axis=1,delta_mm=1,rpm=12);self.c.step()
        self.assertEqual(signals,['now']);self.c.step()
        self.assertTrue(self.c.snapshot()['estop_confirmed'])
    def test_latch_persists_across_controller_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'events.jsonl'
            first=Controller(DemoTransport,log_path=path,autostart=False)
            first.submit('emergency_stop',{'client_id':'viewer'});first.close()
            second=Controller(DemoTransport,log_path=path,autostart=False)
            self.assertTrue(second.snapshot()['estop_latched'])
            self.assertFalse(second.auto_connect);second.close()
    def test_repeat_estop_does_not_interrupt_confirmation(self):
        self.ready_all();signals=[]
        self.transport.emergency_stop=lambda:signals.append(1)
        self.post('emergency_stop');self.post('emergency_stop')
        self.assertEqual(signals,[1])
    def test_reset_three_targets_at_30_rpm(self):
        self.ready_all()
        self.post('reset_positions',moves=[{'axis':1,'target_mm':55},{'axis':2,'target_mm':99},{'axis':3,'target_mm':102}],rpm=30,confirmed=True)
        self.run_job()
        s=self.c.snapshot()
        self.assertEqual(s['job']['state'],'done')
        self.assertTrue(s['job']['reset'])
        self.assertEqual([a['position_mm'] for a in s['axes']],[55,99,102])
        starts=[a for op,a in self.transport.calls if op=='reset_positions']
        self.assertEqual(len(starts),1)
        self.assertEqual(starts[0]['rpm'],30)
        self.assertFalse(any(op=='jog' for op,a in self.transport.calls))
    def test_reset_noop_axis_does_not_divide_by_zero_or_move(self):
        self.ready_all()
        self.post('reset_positions',moves=[{'axis':1,'target_mm':51},{'axis':2,'target_mm':100},{'axis':3,'target_mm':101}],rpm=30,confirmed=True)
        self.run_job()
        self.assertEqual(self.c.snapshot()['job']['state'],'done')
        self.assertFalse(any(op=='jog' for op,a in self.transport.calls))
    def test_stop_continuous_reset_preserves_positions_and_accepts_new_job(self):
        self.ready_all();self.transport.clock=self.clock
        self.post('reset_positions',moves=[{'axis':i,'target_mm':120} for i in (1,2,3)],rpm=30,confirmed=True)
        self.c.step();self.clock.now+=0.4
        self.post('stop');self.c.step()
        self.assertFalse(self.transport.state['reset']['active'])
        self.assertTrue(all(a['anchored'] for a in self.c.snapshot()['axes']))
        self.assertFalse(any(op=='jog' for op,a in self.transport.calls))
        self.post('jog',axis=1,delta_mm=1,rpm=12);self.c.step()
        self.assertEqual(self.c.snapshot()['job']['state'],'running')
    def test_reset_requires_capable_firmware(self):
        self.ready_all()
        self.c.state['capabilities'].remove('continuous_reset')
        with self.assertRaises(APIError):
            self.post('reset_positions',moves=[{'axis':i,'target_mm':120} for i in (1,2,3)],rpm=30,confirmed=True)
        self.assertIsNone(self.c.snapshot()['job'])
    def test_reset_requires_three_valid_targets_and_confirmation(self):
        self.ready_all()
        for moves,confirmed in (([{'axis':1,'target_mm':50}],True),([{'axis':i,'target_mm':400} for i in (1,2,3)],True),([{'axis':i,'target_mm':50} for i in (1,2,3)],False)):
            with self.assertRaises(APIError):self.post('reset_positions',moves=moves,rpm=30,confirmed=confirmed)
        self.assertIsNone(self.c.snapshot()['job'])

class EmergencyTransportTests(unittest.TestCase):
    def test_interrupt_byte_bypasses_rpc_and_blocks_future_commands(self):
        class Serial:
            writes=[]
            in_waiting=0
            def write(self,data):self.writes.append(data);return len(data)
            def flush(self):pass
            def close(self):pass
        t=SerialTransport();t.serial=Serial();t.session='test'
        t.emergency_stop();self.assertEqual(t.serial.writes,[b'\x03'])
        with self.assertRaises(ConnectionFailure):t.request('jog',{'axis':1})
        self.assertEqual(t.serial.writes,[b'\x03'])
        t._buffer.extend(json.dumps({'event':'emergency_stopped','session':'test','confirmed':True}).encode()+b'\n')
        self.assertTrue(t.finish_emergency()['confirmed']);t.close()
