import io
import json
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"firmware/lib"))
from enzyflow.motor_web import MotorRPC, run
from test_motor_safety import FakeClock
from test_syringe_pump import FlowBus

class EmergencyTests(unittest.TestCase):
    def test_all_axes_braked_before_any_clear_and_failure_does_not_skip_axes(self):
        clock=FakeClock();bus=FlowBus(clock);rpc=MotorRPC(bus,clock)
        bus.fail_stop=2
        result=rpc.emergency_stop()
        self.assertEqual(bus.stops[:3],[1,2,3])
        self.assertNotIn(2,bus.clears)
        self.assertFalse(result['confirmed']);self.assertIn(2,result['failed_axes'])
    def test_keyboard_interrupt_in_actual_run_emits_verified_stop(self):
        clock=FakeClock();bus=FlowBus(clock)
        class Input:
            def read(self,n):raise KeyboardInterrupt()
        output=io.StringIO()
        run(bus=bus,clock=clock,stream_in=Input(),stream_out=output)
        rows=[json.loads(line) for line in output.getvalue().splitlines() if line]
        self.assertEqual(rows[-1]['event'],'emergency_stopped')
        self.assertTrue(rows[-1]['confirmed'])
        self.assertEqual(bus.stops,[1,2,3])
        self.assertEqual(bus.clears,[])

    def test_interrupt_bypasses_normal_rpc_failure_cleanup(self):
        clock=FakeClock();bus=FlowBus(clock);rpc=MotorRPC(bus,clock,session='priority')
        def interrupted(op,args):raise KeyboardInterrupt()
        rpc._dispatch=interrupted
        def wrong_cleanup(*args):self.fail('Normal failure cleanup delayed emergency')
        rpc.failure=wrong_cleanup
        with self.assertRaises(KeyboardInterrupt):
            rpc.handle(dict(id='1',seq=1,session='priority',op='status',args={}))
    def test_broadcast_precedes_disable_and_reads_without_clear(self):
        events=[]
        class Bus(FlowBus):
            def _brake_all(self):events.append('broadcast-brake');super()._brake_all()
            def _disable_all(self):events.append('broadcast-disable');super()._disable_all()
            def _disable(self,i):events.append(('disable',i));super()._disable(i)
            def read_pulses(self,i):events.append(('read',i));return super().read_pulses(i)
            def _clear_brake(self,i):self.fail('Must not release emergency brake')
        c=FakeClock();b=Bus(c);b.rates={2:-.8,3:-1.6}
        r=MotorRPC(b,c).emergency_stop()
        self.assertTrue(r['confirmed']);self.assertTrue(r['disabled'])
        self.assertEqual(events[:2],['broadcast-brake','broadcast-disable'])
        self.assertFalse(b.rates)
    def test_fault_details_identify_axis_and_stage(self):
        c=FakeClock();b=FlowBus(c);b.fail_stop=2
        result=MotorRPC(b,c).emergency_stop()
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['details'][0]['axis'],2)
        self.assertEqual(result['details'][0]['stage'],'disable/read')
