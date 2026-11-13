"""Driver recovery fault injection; never opens a physical UART."""
import unittest
from enzyflow.motor_web import MotorRPC
from test_syringe_pump import FlowBus
from test_motor_safety import FakeClock
import test_continuous_reset as continuous_fixture

class DriverBus(FlowBus):
    def __init__(self,clock):
        super().__init__(clock)
        self.disabled={1:1,2:1,3:1};self.state=0;self.stall=0;self.enables=[];self.resume=False
    def read_driver_flag(self,i,op):
        return {0x2C:self.state,0x2D:self.stall,0x2F:self.disabled[i]}[op]
    def _enable(self,i):
        self.enables.append(i);self.disabled[i]=0
        if self.resume:self.rates[i]=30
    def _disable_all(self):
        super()._disable_all();self.disabled={1:1,2:1,3:1}
    def _disable(self,i):
        super()._disable(i);self.disabled[i]=1

class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.clock=FakeClock();self.bus=DriverBus(self.clock);self.rpc=MotorRPC(self.bus,self.clock,session='test')
    def test_requires_explicit_confirmation(self):
        with self.assertRaises(ValueError):self.rpc.restore_drives(False)
        self.assertEqual(self.bus.enables,[])
    def test_restore_acknowledges_all_axes_without_trajectory(self):
        self.rpc.restore_drives(True)
        self.assertEqual(self.bus.enables,[1,2,3]);self.assertEqual(self.bus.clears,[1,2,3])
        self.assertEqual(self.bus.starts,[]);self.assertFalse(self.bus.rates)
        self.assertEqual([d['disabled'] for d in self.rpc.driver_states],[0,0,0])
    def test_faults_refuse_enable_and_leave_all_disabled(self):
        for state,stall in ((2,0),(3,0),(4,1),(5,0),(0,1),(8,0)):
            with self.subTest(state=state,stall=stall):
                self.setUp();self.bus.state=state;self.bus.stall=stall
                with self.assertRaises(ValueError):self.rpc.restore_drives(True)
                self.assertEqual(self.bus.enables,[]);self.assertEqual(list(self.bus.disabled.values()),[1,1,1])
    def test_old_trajectory_resume_is_detected_and_all_axes_disabled(self):
        self.bus.resume=True
        with self.assertRaisesRegex(ValueError,'编码器变化'):self.rpc.restore_drives(True)
        self.assertEqual(self.bus.enables,[1]);self.assertEqual(list(self.bus.disabled.values()),[1,1,1]);self.assertFalse(self.bus.rates)
    def test_ack_failure_disables_already_enabled_axis(self):
        enable=self.bus._enable
        def fail(i):
            if i==2:raise OSError('lost ACK')
            enable(i)
        self.bus._enable=fail
        with self.assertRaises(OSError):self.rpc.restore_drives(True)
        self.assertEqual(list(self.bus.disabled.values()),[1,1,1])

class StopTests(unittest.TestCase):
    def test_stop_preserves_measured_position_and_new_command(self):
        f=continuous_fixture.ContinuousResetTests();f.setUp();f.start();f.clock.sleep_ms(200);f.rpc.tick()
        state=f.call('stop_motion',{})
        self.assertTrue(all(a['anchored'] for a in state['axes']))
        self.assertFalse(f.bus.rates)
        count=len(f.bus.reset_moves);f.clock.sleep_ms(1000);f.rpc.tick()
        self.assertEqual(len(f.bus.reset_moves),count)
        f.start((110,100,100));self.assertGreater(len(f.bus.reset_moves),count)
