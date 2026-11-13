import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'firmware/lib'))
import unittest
from enzyflow.motor_web import MotorRPC
from enzyflow.motor_bus import MotorBus
from test_motor_safety import FakeClock
from test_syringe_pump import FlowBus
from test_motor_bus import UART,Clock

class ResetBus(FlowBus):
    def __init__(self,clock):
        super().__init__(clock);self.targets={};self.reset_moves=[]
    def update(self):
        dt=self.clock.now-self.at
        for i in list(self.rates):
            next_count=self.counts[i]+self.rates[i]*51200*dt/60000
            if i in self.targets and ((self.rates[i]>0 and next_count>=self.targets[i]) or (self.rates[i]<0 and next_count<=self.targets[i])):
                self.counts[i]=self.targets[i];self.rates.pop(i)
            else:self.counts[i]=next_count
        self.at=self.clock.now
    def _move_reset_pulses(self,i,p,rpm=30):
        self.update();self.reset_moves.append((i,p,rpm));self.targets[i]=self.counts[i]+p
        self.rates[i]=rpm*(1 if p>0 else -1)
        if i==self.fail_start:raise OSError('ambiguous full move')

class ContinuousResetTests(unittest.TestCase):
    def setUp(self):
        self.clock=FakeClock();self.bus=ResetBus(self.clock);self.rpc=MotorRPC(self.bus,self.clock,session='reset-test');self.seq=0
        self.call('configure',{'ids':[1,2,3],'long_id':1,'confirmed':True})
        for i in (1,2,3):self.call('anchor',{'axis':i,'mm':100,'sign':-1,'lead':4,'confirmed':True})
    def call(self,op,args):
        self.seq+=1;r=self.rpc.handle(dict(id=str(self.seq),seq=self.seq,session='reset-test',op=op,args=args))
        self.assertTrue(r['ok'],r);return r['result']
    def start(self,targets=(120,90,105)):
        return self.call('reset_positions',{'moves':[{'axis':i,'target_mm':t} for i,t in zip((1,2,3),targets)],'rpm':30,'confirmed':True})
    def finish(self):
        for i in range(1000):
            self.clock.sleep_ms(50);self.rpc.tick()
            if i%5==0:self.call('reset_heartbeat',{})
            if not self.rpc.reset.active:return
        self.fail('reset never completes')
    def test_whole_moves_once_at_30_and_independent_completion(self):
        self.start()
        self.assertEqual(self.bus.reset_moves,[(1,-256000,30),(2,128000,30),(3,-64000,30)])
        for i in range(65):
            self.clock.sleep_ms(50);self.rpc.tick()
            if i%5==0:self.call('reset_heartbeat',{})
        self.assertTrue(self.rpc.reset.members[2]['done']);self.assertFalse(self.rpc.reset.members[0]['done'])
        self.assertTrue(self.rpc.reset.active)
        self.finish();self.assertEqual(self.rpc.reset.result,'done')
        self.assertEqual(len(self.bus.reset_moves),3)
        self.assertEqual([a['position_mm'] for a in self.call('status',{})['axes']],[120,90,105])
        self.assertEqual(self.bus.stops,[])  # Driver stopped at finite target.
    def test_all_targets_validate_before_first_write(self):
        with self.assertRaises(ValueError):self.rpc.reset.start([{'axis':1,'target_mm':110},{'axis':2,'target_mm':110},{'axis':3,'target_mm':400}],30,True)
        self.assertEqual(self.bus.reset_moves,[])
    def test_duplicate_request_never_restarts_or_accumulates_distance(self):
        self.seq+=1;request=dict(id=str(self.seq),seq=self.seq,session='reset-test',op='reset_positions',args={'moves':[{'axis':i,'target_mm':120} for i in (1,2,3)],'rpm':30,'confirmed':True})
        self.assertTrue(self.rpc.handle(request)['ok']);self.assertTrue(self.rpc.handle(request)['ok'])
        self.assertEqual(len(self.bus.reset_moves),3)
    def test_partial_start_failure_brakes_all_and_never_retries(self):
        self.bus.fail_start=2
        with self.assertRaises(OSError):self.rpc.reset.start([{'axis':i,'target_mm':120} for i in (1,2,3)],30,True)
        self.assertEqual(len(self.bus.reset_moves),2);self.assertEqual(self.bus.stops,[1,2,3]);self.assertFalse(self.bus.rates)
    def test_manual_stop_and_timeout_brake_whole_moves(self):
        for timeout in (False,True):
            self.setUp();self.start()
            if timeout:self.clock.sleep_ms(1501);self.rpc.tick()
            else:self.call('lock',{})
            self.assertFalse(self.rpc.reset.active);self.assertEqual(self.bus.stops,[1,2,3]);self.assertFalse(self.bus.rates)
    def test_stall_and_wrong_direction_abort(self):
        for rate in (0,30):
            self.setUp();self.start();self.bus.rates[1]=rate
            self.finish();self.assertNotEqual(self.rpc.reset.result,'done');self.assertFalse(self.bus.rates)
    def test_already_at_target_skips_axes(self):
        self.start((100,100,101));self.finish()
        self.assertEqual(self.bus.reset_moves,[(3,-12800,30)])
        self.assertEqual(self.rpc.reset.result,'done')
    def test_stop_failure_is_reported_and_other_axes_are_braked(self):
        self.start();self.bus.fail_stop=1;self.call('lock',{})
        self.assertTrue(self.rpc.reset.stop_fault);self.assertIn(3,self.bus.stops)
    def test_emergency_cancels_continuous_supervisor(self):
        self.start();r=self.rpc.emergency_stop()
        self.assertTrue(r['confirmed']);self.assertFalse(self.rpc.reset.active);self.assertFalse(self.bus.rates)
    def test_long_slow_move_progress_does_not_trip_rpc_timeout(self):
        self.call('reset_positions',{'moves':[{'axis':i,'target_mm':110} for i in (1,2,3)],'rpm':3,'confirmed':True})
        for _ in range(60):self.clock.sleep_ms(300);self.call('reset_heartbeat',{})
        self.assertTrue(self.rpc.reset.active);self.assertEqual(len(self.bus.reset_moves),3)

class FullPositionPacketTests(unittest.TestCase):
    def test_full_count_and_speed_encoded_without_weakening_jog_limit(self):
        clock=Clock();uart=UART(clock);bus=MotorBus(uart,clock=clock)
        bus._move_reset_pulses(2,-256000,30)
        p=uart.writes[0];self.assertEqual(p[2],0xF3);self.assertEqual(p[3],1)
        self.assertEqual(int.from_bytes(p[7:11],'big'),256000);self.assertEqual(int.from_bytes(p[5:7],'big'),30)
        self.assertEqual(p[-2],sum(p[:-2])&255)
        with self.assertRaises(ValueError):bus._move_pulses(2,-256000,30)
        self.assertEqual(len(uart.writes),1)
