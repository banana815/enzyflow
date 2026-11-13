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
clock=Clock();bus=Bus(clock);rpc=MotorRPC(bus,clock,session='pump-offline');seq=0

def call(op,args):
    global seq
    seq+=1
    r=rpc.handle({'id':str(seq),'seq':seq,'session':rpc.session,'op':op,'args':args})
    assert r['ok'],r
    return r
call('configure',{'ids':[1,2,3],'long_id':1,'confirmed':True})
for i in (2,3):call('anchor',{'axis':i,'mm':100,'sign':-1,'lead':4,'confirmed':True})
call('pump_start',{'items':[{'axis':i,'direction':1,'remaining_mm':10} for i in (2,3)],'confirmed':True})
for _ in range(200):
    clock.sleep_ms(300);call('pump_heartbeat',{})
assert bus.starts==[2,3]
assert all(abs(m['volume_ml']-1)<.001 for m in rpc.pump.members)
clock.sleep_ms(1501);rpc.pump.tick()
assert not rpc.pump.active and not bus.rates and bus.stops==[2,3]
print('BOARD_PUMP_FAKE_PASS: each 1mL/min, watchdog stops both, no real UART')
