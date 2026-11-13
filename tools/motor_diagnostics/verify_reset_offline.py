"""Run on MicroPython with simulated counters only; never constructs UART."""
from enzyflow.motor_web import MotorRPC
class Clock:
    now=0
    def ticks_ms(self):return self.now
    def ticks_diff(self,a,b):return a-b
    def sleep_ms(self,ms):self.now+=ms
class Bus:
    def __init__(self,c):
        self.c=c;self.at=c.now;self.counts={1:0,2:0,3:0};self.rates={};self.targets={};self.moves=[];self.stops=[]
    def update(self):
        dt=self.c.now-self.at
        for i in list(self.rates):
            v=self.counts[i]+self.rates[i]*51200*dt/60000
            if (self.rates[i]>0 and v>=self.targets[i]) or (self.rates[i]<0 and v<=self.targets[i]):
                v=self.targets[i];del self.rates[i]
            self.counts[i]=v
        self.at=self.c.now
    def read_pulses(self,i):self.update();return int(round(self.counts[i]))
    def _move_reset_pulses(self,i,p,rpm=30):
        self.update();self.moves.append((i,p,rpm));self.targets[i]=self.counts[i]+p;self.rates[i]=rpm*(1 if p>0 else -1)
    def _brake(self,i):self.update();self.stops.append(i);self.rates.pop(i,None)
    def _clear_brake(self,i):pass
c=Clock();b=Bus(c);r=MotorRPC(b,c,session='reset-offline')
r.controller.configure([1,2,3],1,True)
for i in (1,2,3):r.controller.anchor(i,100,-1,4,True)
moves=[{'axis':i,'target_mm':100+i} for i in (1,2,3)]
r.reset.start(moves,30,True)
for _ in range(200):
    c.sleep_ms(50);r.reset.heartbeat()
    if not r.reset.active:break
assert r.reset.result=='done' and len(b.moves)==3 and not b.stops
assert [r.controller.position(i) for i in (1,2,3)]==[101,102,103]
r.reset.start([{'axis':i,'target_mm':120} for i in (1,2,3)],30,True)
c.sleep_ms(1501);r.tick()
assert not r.reset.active and b.stops==[1,2,3] and not b.rates
print('BOARD_CONTINUOUS_RESET_FAKE_PASS: finite move once, independent arrival, lease brake; no UART')
