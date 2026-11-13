"""MicroPython fake-bus proof; never constructs machine.UART."""
from enzyflow.motor_web import MotorRPC
class Clock:
    now=0
    def ticks_ms(self):return self.now
    def ticks_diff(self,a,b):return a-b
    def sleep_ms(self,ms):self.now+=ms
class Bus:
    def __init__(self,clock):
        self.clock=clock;self.at=clock.now;self.counts={1:0,2:0,3:0};self.rates={};self.starts=[];self.stops=[]
    def update(self):
        dt=self.clock.now-self.at
        for i in self.rates:self.counts[i]+=self.rates[i]*51200*dt/60000
        self.at=self.clock.now
    def read_pulses(self,i):self.update();return int(round(self.counts[i]))
    def _velocity(self,i,rpm):self.update();self.starts.append(i);self.rates[i]=rpm
    def _brake(self,i):self.update();self.stops.append(i);self.rates.pop(i,None)
    def _clear_brake(self,i):pass
    def _brake_all(self):
        self.events=['broadcast-brake']
        for i in (1,2,3):self._brake(i)
    def _disable_all(self):
        self.events.append('broadcast-disable');self.rates.clear()
    def _disable(self,i):
        self.events.append(('disable',i));self.rates.pop(i,None)
    def _clear_brake(self,i):raise AssertionError('Emergency must not clear brake')
clock=Clock();bus=Bus(clock);rpc=MotorRPC(bus,clock,session='pump-offline');seq=0

def call(op,args):
    global seq
    seq+=1
    r=rpc.handle({'id':str(seq),'seq':seq,'session':rpc.session,'op':op,'args':args})
    assert r['ok'],r
    return r
call('configure',{'ids':[1,2,3],'long_id':1,'confirmed':True})
for i in (2,3):call('anchor',{'axis':i,'mm':100,'sign':-1,'lead':4,'confirmed':True})
call('pump_start',{'items':[{'axis':i,'direction':1,'remaining_mm':10,'flow_ml_min':flow} for i,flow in ((2,.5),(3,2))],'confirmed':True})
for _ in range(200):
    clock.sleep_ms(300);call('pump_heartbeat',{})
assert bus.starts==[2,3]
assert all(abs(m['volume_ml']-f)<.001 for m,f in zip(rpc.pump.members,(.5,2)))
assert rpc.pump.state()['total_flow_ml_min']==2.5
result=rpc.emergency_stop()
assert result['confirmed'] and result['disabled'] and not bus.rates
assert bus.events[:2]==['broadcast-brake','broadcast-disable']
assert bus.stops==[1,2,3]
print('BOARD_PRIORITY_STOP_PASS: independent feed interrupted, broadcast first, disabled, no clear; fake bus only')
