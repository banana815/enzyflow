"""Host scheduling against real firmware RPC on a timed fake motor bus."""
import sys
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'firmware/lib'),str(ROOT/'tests/firmware')]
from enzyflow.motor_web import MotorRPC
from test_syringe_pump import FlowBus
from test_motor_safety import FakeClock
from webcontrol.controller import Controller, APIError
from webcontrol.transport import DeviceError

class RPCTransport:
    port='fake-wire'
    device='fake-board'
    def __init__(self,clock):self.bus=FlowBus(clock);self.rpc=MotorRPC(self.bus,clock,session='pump-test');self.seq=0;self.session=None
    def connect(self):self.session=self.rpc.session;return self.rpc.state()
    def request(self,op,args=None):
        self.seq+=1
        r=self.rpc.handle(dict(id=str(self.seq),seq=self.seq,session=self.session,op=op,args=args or {}))
        if not r['ok']:raise DeviceError(r['error']['message'])
        return r['result']
    def close(self,graceful=False):self.rpc.lock();self.session=None

class HostPumpTests(unittest.TestCase):
    def setUp(self):
        self.clock=FakeClock();self.t=RPCTransport(self.clock)
        self.c=Controller(lambda:self.t,clock=lambda:self.clock.now/1000,autostart=False);self.n=0
        self.post('claim');self.c.step()
        self.post('configure',ids=[1,2,3],long_id=1,confirmed=True);self.c.step()
        for i in (2,3):self.post('anchor',axis=i,mm=100,sign=-1,lead=4,confirmed=True);self.c.step()
        self.post('heartbeat')
    def tearDown(self):self.c.close()
    def post(self,op,**args):
        self.n+=1;return self.c.submit(op,dict(client_id='page',request_id=str(self.n),**args))
    def start(self):
        return self.post('pump_start',items=[dict(axis=i,direction=1,remaining_mm=10) for i in (2,3)],confirmed=True)
    def test_continuous_worker_heartbeats_and_user_stop(self):
        self.start();self.c.step()
        self.assertEqual(self.c.snapshot()['job']['kind'],'pump')
        for _ in range(200):
            self.clock.sleep_ms(300);self.post('heartbeat');self.c.step()
        self.assertEqual(len(self.t.bus.starts),2)
        self.assertAlmostEqual(self.c.snapshot()['job']['members'][0]['volume_ml'],1,places=4)
        self.post('stop');self.c.step()
        self.assertFalse(self.t.bus.rates);self.assertEqual(self.c.snapshot()['job']['state'],'cancelled')
    def test_independent_rates_reach_firmware_and_job(self):
        self.post('pump_start',items=[dict(axis=i,direction=1,remaining_mm=10,flow_ml_min=f) for i,f in ((2,.5),(3,2))],confirmed=True)
        self.c.step()
        for _ in range(200):
            self.clock.sleep_ms(300);self.post('heartbeat');self.c.step()
        members=self.c.snapshot()['job']['members']
        for m,f in zip(members,(.5,2)):
            self.assertEqual(m['flow_ml_min'],f)
            self.assertAlmostEqual(m['volume_ml'],f,places=4)
        self.assertEqual(len(self.t.bus.starts),2)
    def test_invalid_rate_or_old_firmware_rejected_before_start(self):
        for flow in (0,-1,3,True):
            with self.assertRaises(APIError):
                self.post('pump_start',items=[dict(axis=i,direction=1,remaining_mm=10,flow_ml_min=f) for i,f in ((2,.5),(3,flow))],confirmed=True)
        self.c.state['capabilities'].remove('pump_rates')
        with self.assertRaises(APIError):self.start()
        self.assertEqual(self.t.bus.starts,[])
    def test_page_lease_loss_brakes_both_axes(self):
        self.start();self.c.step();self.clock.sleep_ms(6000);self.c.step()
        self.assertFalse(self.t.bus.rates);self.assertEqual(self.t.bus.stops,[2,3])
    def test_different_motion_rejected_during_pump(self):
        self.start();self.c.step()
        with self.assertRaises(APIError):self.post('jog',axis=2,delta_mm=.5,rpm=6)
    def test_missing_remaining_or_long_axis_rejected_before_uart(self):
        for items in ([dict(axis=1,direction=1,remaining_mm=10),dict(axis=3,direction=1,remaining_mm=10)],
                      [dict(axis=2,direction=1,remaining_mm=0),dict(axis=3,direction=1,remaining_mm=10)]):
            with self.assertRaises(APIError):self.post('pump_start',items=items,confirmed=True)
        self.assertEqual(self.t.bus.starts,[])
    def test_disconnect_brakes_before_serial_close(self):
        self.start();self.c.step();self.post('disconnect');self.c.step()
        self.assertEqual(self.t.bus.stops,[2,3]);self.assertFalse(self.c.snapshot()['connected'])
    def test_duplicate_start_response_does_not_queue_another_feed(self):
        body=dict(client_id='page',request_id='unique-pump',items=[dict(axis=i,direction=1,remaining_mm=10) for i in (2,3)],confirmed=True)
        self.c.submit('pump_start',body);self.c.step()
        self.assertTrue(self.c.submit('pump_start',body)['duplicate']);self.c.step()
        self.assertEqual(len(self.t.bus.starts),2)
