import math
import struct
import unittest
from enzyflow.motor_web import MotorRPC, _read_line
from enzyflow.syringe_pump import AREA_MM2, MM_MIN
from enzyflow.motor_bus import MotorBus
from test_motor_safety import FakeClock
from test_motor_bus import UART, Clock

class FlowBus:
    def __init__(self,clock):
        self.clock=clock; self.counts={1:0,2:0,3:0}; self.rates={}; self.at=clock.now
        self.starts=[]; self.stops=[]; self.clears=[]; self.fail_start=None; self.fail_stop=None
    def update(self):
        dt=self.clock.now-self.at
        for i,rpm in self.rates.items(): self.counts[i]+=rpm*51200*dt/60000
        self.at=self.clock.now
    def read_pulses(self,i): self.update(); return round(self.counts[i])
    def _velocity(self,i,rpm):
        self.update();self.starts.append((i,rpm));self.rates[i]=rpm
        if i==self.fail_start: raise OSError('ambiguous start')
    def _brake(self,i):
        self.update();self.stops.append(i)
        if i==self.fail_stop: raise OSError('failed brake')
        self.rates.pop(i,None)
    def _clear_brake(self,i): self.clears.append(i)

    def _brake_all(self):
        for i in (1,2,3):
            try:self._brake(i)
            except OSError:pass
    def _disable_all(self):
        self.update();self.rates.clear()
    def _disable(self,i):
        if i==self.fail_stop:raise OSError('disable ACK missing')
        self.update();self.rates.pop(i,None)

class PumpTests(unittest.TestCase):
    def setUp(self):
        self.clock=FakeClock();self.bus=FlowBus(self.clock);self.rpc=MotorRPC(self.bus,self.clock,session='test');self.seq=0
        self.call('configure',{'ids':[1,2,3],'long_id':1,'confirmed':True})
        for i in (2,3): self.call('anchor',{'axis':i,'mm':100,'sign':-1,'lead':4,'confirmed':True})
    def call(self,op,args):
        self.seq+=1
        r=self.rpc.handle({'id':str(self.seq),'seq':self.seq,'session':'test','op':op,'args':args})
        self.assertTrue(r['ok'],r)
        return r
    def items(self,remaining=10):
        return [{'axis':i,'direction':1,'remaining_mm':remaining} for i in (2,3)]
    def start(self,remaining=10): return self.call('pump_start',{'items':self.items(remaining),'confirmed':True})
    def test_one_ml_minute_each_without_restarting(self):
        self.start()
        for _ in range(200):
            self.clock.sleep_ms(300);self.call('pump_heartbeat',{})
        self.assertEqual(len(self.bus.starts),2)
        self.assertAlmostEqual(self.rpc.pump.members[0]['volume_ml'],1,places=4)
        self.assertAlmostEqual(self.rpc.pump.members[1]['volume_ml'],1,places=4)
        self.assertAlmostEqual(abs(self.bus.starts[0][1]),0.8616747377,places=8)
        self.assertTrue(self.rpc.pump.active)
    def test_independent_half_and_double_rate_one_minute(self):
        items=self.items()
        for item,flow in zip(items,(0.5,2.0)):item['flow_ml_min']=flow
        self.call('pump_start',{'items':items,'confirmed':True})
        for _ in range(200):
            self.clock.sleep_ms(300);self.call('pump_heartbeat',{})
        self.assertTrue(self.rpc.pump.active)
        self.assertEqual(len(self.bus.starts),2)
        for m,flow in zip(self.rpc.pump.members,(0.5,2.0)):
            self.assertAlmostEqual(m['volume_ml'],flow,places=4)
            self.assertAlmostEqual(m['rpm'],MM_MIN*flow/4)
        self.assertEqual(self.rpc.pump.state()['total_flow_ml_min'],2.5)
    def test_invalid_second_flow_never_starts_either_axis(self):
        for flow in (0,-1,0.01,3,float('nan'),float('inf'),True,'1',None):
            items=self.items();items[1]['flow_ml_min']=flow
            with self.assertRaises(ValueError):self.rpc.pump.start(items,1,True)
        self.assertEqual(self.bus.starts,[])
    def test_fast_axis_end_stops_both_at_independent_rates(self):
        items=self.items(1.5)
        for item,flow in zip(items,(0.5,2.0)):item['flow_ml_min']=flow
        self.call('pump_start',{'items':items,'confirmed':True})
        for _ in range(40):
            self.clock.sleep_ms(300);self.call('pump_heartbeat',{})
            if not self.rpc.pump.active:break
        self.assertFalse(self.rpc.pump.active)
        self.assertEqual(self.bus.stops,[2,3])
        a,b=self.rpc.pump.members
        self.assertAlmostEqual(b['volume_ml']/a['volume_ml'],4,places=2)
        self.assertIn('到达设定行程',self.rpc.pump.result)
    def test_segmented_axes_change_speed_and_finish_independently(self):
        items = self.items()
        plans = [((0.5, 1), (0.5, 2)), ((1.5, 0.5),)]
        for item, plan in zip(items, plans):
            item.update(flow_ml_min=plan[0][1],
                        target_volume_ml=sum(d for d, _ in plan)*AREA_MM2/1000,
                        segments=[dict(distance_mm=d, flow_ml_min=f) for d, f in plan])
        self.call('pump_start', {'items': items, 'confirmed':True})
        for _ in range(500):
            self.clock.sleep_ms(250)
            self.call('pump_heartbeat', {})
            if not self.rpc.pump.active:
                break
            if self.rpc.pump.members[0]['done']:
                self.assertNotIn(2, self.bus.rates)
                self.assertIn(3, self.bus.rates)
        self.assertIn('均已到达设定行程', self.rpc.pump.result)
        self.assertFalse(self.bus.rates)
        self.assertEqual([i for i, _ in self.bus.starts], [2, 3, 2])
        for member, item in zip(self.rpc.pump.members, items):
            self.assertAlmostEqual(member['volume_ml'], item['target_volume_ml'], delta=0.02)

    def test_estop_returns_new_encoder_position_before_lock(self):
        self.start()
        self.clock.sleep_ms(1000)
        result = self.rpc.emergency_stop()
        self.assertTrue(result['confirmed'])
        self.assertEqual([a['id'] for a in result['stopped_axes']], [2, 3])
        for axis in result['stopped_axes']:
            self.assertAlmostEqual(axis['position_mm'], 100 + MM_MIN/60, places=3)
        self.assertTrue(all(a.reference is None for a in self.rpc.controller.axes.values()))

    def test_segmented_invalid_second_axis_prevents_all_motion(self):
        items = self.items()
        for item in items:
            item.update(flow_ml_min=1, target_volume_ml=AREA_MM2/1000,
                        segments=[dict(distance_mm=1, flow_ml_min=1)])
        for distance in (0.3, 2, 20):
            items[1]['segments'][0]['distance_mm'] = distance
            with self.assertRaises(ValueError):
                self.rpc.pump.start(items, 1, True)
            self.assertFalse(self.bus.starts)

    def test_speed_range_uses_each_axes_lead(self):
        self.call('anchor',{'axis':3,'mm':100,'sign':-1,'lead':2,'confirmed':True})
        items=self.items();items[1]['flow_ml_min']=2
        with self.assertRaises(ValueError):self.rpc.pump.start(items,1,True)
        self.assertEqual(self.bus.starts,[])
        items[1]['flow_ml_min']=1
        self.call('pump_start',{'items':items,'confirmed':True})
        self.assertAlmostEqual(abs(self.bus.starts[1][1]),MM_MIN/2)
    def test_brake_failure_never_clears_and_disables_instead(self):
        self.start()
        # Model a driver that ignored brake: old code released it unconditionally.
        self.bus._brake=lambda i:self.bus.stops.append(i)
        self.call('lock',{})
        self.assertTrue(self.rpc.pump.stop_fault)
        self.assertFalse(self.bus.clears)
        self.assertFalse(self.bus.rates)
    def test_clear_resuming_feed_is_detected_and_disabled(self):
        self.start()
        def resume(i):
            self.bus.clears.append(i);self.bus.rates[i]=-MM_MIN/4
        self.bus._clear_brake=resume
        self.call('lock',{})
        self.assertTrue(self.rpc.pump.stop_fault)
        self.assertFalse(self.bus.rates)
    def test_watchdog_stops_without_host_request(self):
        self.start();self.clock.sleep_ms(1501);self.rpc.pump.tick()
        self.assertEqual(self.bus.stops,[2,3]);self.assertFalse(self.rpc.pump.active)
        self.assertFalse(self.bus.rates)
        self.assertTrue(all(a.reference is None for a in self.rpc.controller.axes.values()))
    def test_late_heartbeat_does_not_revive(self):
        self.start();self.clock.sleep_ms(1600);r=self.call('pump_heartbeat',{})
        self.assertFalse(r['result']['pump']['active']);self.assertEqual(len(self.bus.starts),2)
    def test_manual_stop_brakes_both_and_locks(self):
        self.start();self.call('lock',{})
        self.assertEqual(self.bus.stops,[2,3]);self.assertEqual(self.bus.clears,[2,3]);self.assertFalse(self.bus.rates)
    def test_invalid_second_axis_does_not_start_first(self):
        items=self.items();items[1]['remaining_mm']=0
        with self.assertRaises(ValueError):self.rpc.pump.start(items,1,True)
        self.assertEqual(self.bus.starts,[])
    def test_unknown_direction_or_missing_confirmation_never_starts(self):
        for direction in (0,True,None):
            items=self.items();items[0]['direction']=direction
            with self.assertRaises(ValueError):self.rpc.pump.start(items,1,True)
        with self.assertRaises(ValueError):self.rpc.pump.start(self.items(),1,False)
        self.assertEqual(self.bus.starts,[])
    def test_ambiguous_second_start_brakes_every_axis(self):
        self.bus.fail_start=3
        with self.assertRaises(OSError):self.rpc.pump.start(self.items(),1,True)
        self.assertEqual(self.bus.stops,[2,3]);self.assertFalse(self.bus.rates)
    def test_stop_failure_does_not_skip_other_motor_or_clear_failed_brake(self):
        self.start();self.bus.fail_stop=2;self.rpc.pump.stop()
        self.assertIn(3,self.bus.stops);self.assertNotIn(2,self.bus.clears)
        self.assertTrue(self.rpc.pump.stop_fault)
        self.assertEqual(self.bus.rates.get(3),None)
    def test_either_syringe_end_stops_both(self):
        self.start(1.5)
        for _ in range(40):
            self.clock.sleep_ms(300);self.call('pump_heartbeat',{})
            if not self.rpc.pump.active:break
        self.assertFalse(self.rpc.pump.active);self.assertFalse(self.bus.rates)
        self.assertIn('到达设定行程',self.rpc.pump.result)
    def test_reverse_and_stall_stop_feed(self):
        for rate in (0,1):
            self.setUp();self.start();self.bus.rates[2]=rate
            for _ in range(30):
                self.clock.sleep_ms(300);self.call('pump_heartbeat',{})
                if not self.rpc.pump.active:break
            self.assertFalse(self.rpc.pump.active);self.assertFalse(self.bus.rates)
    def test_duplicate_start_does_not_start_again(self):
        self.seq+=1
        request={'id':str(self.seq),'seq':self.seq,'session':'test','op':'pump_start','args':{'items':self.items(),'confirmed':True}}
        self.assertTrue(self.rpc.handle(request)['ok']);self.assertTrue(self.rpc.handle(request)['ok'])
        self.assertEqual(len(self.bus.starts),2)
    def test_partial_input_still_runs_watchdog(self):
        self.start()
        class Poller:
            calls=0
            def poll(inner,ms):
                self.clock.sleep_ms(ms);inner.calls+=1
                return [] if inner.calls<100 else [(1,1)]
        class Input:
            def read(inner,n): return b''
        self.assertIsNone(_read_line(Input(),self.rpc.pump,Poller()))
        self.assertFalse(self.rpc.pump.active)

class SpeedPacketTests(unittest.TestCase):
    def test_fractional_speed_and_stop_encoding(self):
        clock=Clock()
        def reply(data):
            p=bytes([0xC5,data[1],data[2],1]);return [(1,p+bytes([sum(p)&255,0x5C]))]
        uart=UART(clock,reply=reply);bus=MotorBus(uart,clock=clock)
        bus._velocity(2,-MM_MIN/4)
        packet=uart.writes[0]
        self.assertEqual(packet[:5],bytes([0xC5,2,0xF1,1,100]))
        self.assertAlmostEqual(struct.unpack('>f',packet[5:9])[0],MM_MIN/4,places=7)
        self.assertEqual(packet[-2],sum(packet[:-2])&255)
        bus._brake(2);self.assertEqual(uart.writes[-1],bytes([0xC5,2,0xFC,0xC3,0x5C]))
        bus._clear_brake(2);self.assertEqual(uart.writes[-1][2],0xFB)
    def test_invalid_velocity_never_writes(self):
        for rpm in (0,.01,3,float('nan'),True,'1'):
            clock=Clock();uart=UART(clock);bus=MotorBus(uart,clock=clock)
            with self.assertRaises(ValueError):bus._velocity(2,rpm)
            self.assertEqual(uart.writes,[])
